import os
import base64
import json
import traceback

from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore, GeoPoint
import requests

# 1. Load environment variables
FIREBASE_KEY_BASE64 = os.environ.get("FIREBASE_KEY_BASE64")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

if not FIREBASE_KEY_BASE64:
    raise Exception("FIREBASE_KEY_BASE64 environment variable not set!")
if not GOOGLE_MAPS_API_KEY:
    raise Exception("GOOGLE_MAPS_API_KEY environment variable not set!")

# 2. Decode and initialize Firebase
try:
    decoded_key = base64.b64decode(FIREBASE_KEY_BASE64).decode("utf-8")
    key_json = json.loads(decoded_key)
    cred = credentials.Certificate(key_json)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    raise Exception(f"Failed to initialize Firebase Admin: {str(e)}")

app = Flask(__name__)

# -------------------------------------------------------------------
# (Optional) Endpoint: Geocode an address on the server
# -------------------------------------------------------------------
@app.route('/geocode-address', methods=['POST'])
def geocode_address():
    try:
        data = request.json
        if not data or 'address' not in data:
            return jsonify({"error": "Address not provided"}), 400

        address = data['address']
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "address": address,
            "key": GOOGLE_MAPS_API_KEY
        }

        response = requests.get(url, params=params)
        response_data = response.json()

        if response.status_code == 200:
            if response_data.get('status') == 'OK':
                location = response_data['results'][0]['geometry']['location']
                lat = float(location['lat'])
                lng = float(location['lng'])
                return jsonify({"latitude": lat, "longitude": lng}), 200
            else:
                # e.g. "ZERO_RESULTS", "REQUEST_DENIED", etc.
                err = response_data.get('status')
                return jsonify({"error": f"Geocoding API error: {err}"}), 400
        else:
            return jsonify({"error": f"Failed to fetch geocoding data. Status code: {response.status_code}"}), 500

    except Exception as e:
        app.logger.error(f"Error in /geocode-address: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------------
# Endpoint: Get Nearest Ambulance
# -------------------------------------------------------------------
@app.route('/get-nearest-ambulance', methods=['POST'])
def get_nearest_ambulance():
    """
    Takes JSON: {"location": {"latitude": xx.x, "longitude": yy.y}}
    Finds the nearest ambulance from Firestore (which stores location as a GeoPoint).
    Returns nearest ambulance data.
    """
    try:
        user_location = request.json.get("location")
        if not user_location:
            return jsonify({"error": "Location not provided"}), 400

        user_lat = user_location['latitude']
        user_lng = user_location['longitude']

        # Fetch ambulance locations from Firebase
        ambulances_ref = db.collection('ambulances')
        ambulances = ambulances_ref.stream()

        ambulance_data = []
        for amb in ambulances:
            amb_dict = amb.to_dict()
            current_location = amb_dict.get('current_location')

            # If stored as a GeoPoint, convert to lat/lng
            if isinstance(current_location, GeoPoint):
                ambulance_data.append({
                    "id": amb_dict.get('ambulance_id', 'Not available'),
                    "latitude": current_location.latitude,
                    "longitude": current_location.longitude,
                    "name": amb_dict.get('driver_name', 'Ambulance'),
                    "contact": amb_dict.get('contact_number', 'Not provided'),
                    "status": amb_dict.get('status', 'Not available')
                })
            else:
                app.logger.warning(f"Ambulance {amb.id} has invalid location data")

        # Use Google Distance Matrix to find the nearest
        min_distance = float('inf')
        nearest_ambulance = None
        nearest_ambulance_time = None

        for ambulance in ambulance_data:
            # Distance Matrix
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                "origins": f"{user_lat},{user_lng}",
                "destinations": f"{ambulance['latitude']},{ambulance['longitude']}",
                "key": GOOGLE_MAPS_API_KEY,
                "mode": "driving",
                "departure_time": "now"
            }

            response = requests.get(url, params=params)
            data = response.json()

            if data.get('status') == 'OK' and data['rows']:
                elements = data['rows'][0]['elements'][0]
                if elements['status'] == 'OK':
                    distance = elements['distance']['value']  # meters
                    duration = elements['duration']['text']
                    if distance < min_distance:
                        min_distance = distance
                        nearest_ambulance = ambulance
                        nearest_ambulance_time = duration
                else:
                    app.logger.warning(f"Google API invalid distance for {ambulance['id']}")
            else:
                error_msg = data.get('error_message', 'No valid distance available')
                app.logger.error(f"Error from Distance Matrix: {error_msg}")

        if nearest_ambulance:
            # Mark 'busy'
            ambulance_id = nearest_ambulance["id"]
            db.collection('ambulances').document(ambulance_id).update({
                'status': 'busy'
            })

            return jsonify({
                "nearest_ambulance": {
                    "id": nearest_ambulance["id"],
                    "latitude": nearest_ambulance["latitude"],
                    "longitude": nearest_ambulance["longitude"],
                    "contact": nearest_ambulance["contact"],
                    "driver_name": nearest_ambulance["name"],
                    "distance_meters": min_distance,
                    "duration": nearest_ambulance_time
                }
            }), 200
        else:
            return jsonify({"error": "No ambulances found or no valid distance data"}), 404

    except Exception as e:
        app.logger.error(f"Error occurred: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------------
# Endpoint: Fetch Route from Ambulance to User
# -------------------------------------------------------------------
@app.route('/fetch-route', methods=['POST'])
def fetch_route():
    """
    Expects JSON: {
      "ambulance_id": "...",
      "user_lat": 123.456,
      "user_lng": 78.90
    }
    Returns: {
      "path": [ [lat1, lng1], [lat2, lng2], ...],
      "distance": "...",
      "duration": "..."
    }
    """
    try:
        data = request.json
        if not data:
            return jsonify({"error": "Request data missing"}), 400

        ambulance_id = data.get("ambulance_id")
        user_lat = data.get("user_lat")
        user_lng = data.get("user_lng")

        if not ambulance_id or user_lat is None or user_lng is None:
            return jsonify({"error": "Missing ambulance_id or user location"}), 400

        # Fetch the ambulance doc
        amb_doc = db.collection('ambulances').document(ambulance_id).get()
        if not amb_doc.exists:
            return jsonify({"error": "Ambulance not found"}), 404

        amb_data = amb_doc.to_dict()
        current_location = amb_data.get("current_location")
        if not isinstance(current_location, GeoPoint):
            return jsonify({"error": "Ambulance has invalid location"}), 400

        ambulance_lat = current_location.latitude
        ambulance_lng = current_location.longitude

        # Use Google Directions API
        directions_url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": f"{ambulance_lat},{ambulance_lng}",
            "destination": f"{user_lat},{user_lng}",
            "key": GOOGLE_MAPS_API_KEY,
            "mode": "driving"
        }
        resp = requests.get(directions_url, params=params)
        directions_data = resp.json()

        if resp.status_code == 200 and directions_data.get("status") == "OK":
            route = directions_data['routes'][0]
            leg = route['legs'][0]

            distance = leg['distance']['text']
            duration = leg['duration']['text']

            # We can decode the polyline on the server or pass it to Flutter. Let's decode on the server:
            polyline_str = route['overview_polyline']['points']
            path_coords = decode_polyline(polyline_str)  # returns [[lat, lng], [lat, lng], ...]

            return jsonify({
                "path": path_coords,
                "distance": distance,
                "duration": duration
            }), 200
        else:
            err_msg = directions_data.get("error_message", "Directions not found")
            return jsonify({"error": f"Directions API error: {err_msg}"}), 400

    except Exception as e:
        app.logger.error(f"Error in /fetch-route: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


def decode_polyline(polyline_str):
    """Decode a polyline string into a list of [lat, lng]"""
    points = []
    index = 0
    lat = 0
    lng = 0
    length = len(polyline_str)

    while index < length:
        shift = 0
        result = 0
        while True:
            b = ord(polyline_str[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        shift = 0
        result = 0
        while True:
            b = ord(polyline_str[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        points.append([lat / 1e5, lng / 1e5])
    return points


# -------------------------------------------------------------------
# Entry point for local or hosting on Railway
# -------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
