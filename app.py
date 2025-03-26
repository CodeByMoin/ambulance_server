import os
import base64
import json
import traceback

from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
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

@app.route('/get-nearest-ambulance', methods=['POST'])
def get_nearest_ambulance():
    try:
        user_location = request.json.get("location")  # {"latitude": xx.x, "longitude": yy.y}
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
            current_location = amb_dict.get('current_location', {})

            if current_location.get('latitude') is not None and current_location.get('longitude') is not None:
                ambulance_data.append({
                    "id": amb_dict.get('ambulance_id', 'Not available'),
                    "latitude": current_location['latitude'],
                    "longitude": current_location['longitude'],
                    "name": amb_dict.get('driver_name', 'Ambulance'),
                    "contact": amb_dict.get('contact_number', 'Not provided'),
                    "status": amb_dict.get('status', 'Not available')
                })
            else:
                app.logger.warning(f"Ambulance {amb.id} is missing location data")

        # Use Google Distance Matrix API to calculate distances
        min_distance = float('inf')
        nearest_ambulance = None
        nearest_ambulance_time = None

        for ambulance in ambulance_data:
            # Build the Distance Matrix request
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                "origins": f"{user_lat},{user_lng}",
                "destinations": f"{ambulance['latitude']},{ambulance['longitude']}",
                "key": GOOGLE_MAPS_API_KEY,
                "mode": "driving",
                "departure_time": "now"
            }

            app.logger.debug(f"Requesting Google Distance Matrix with params: {params}")
            response = requests.get(url, params=params)
            data = response.json()
            app.logger.debug(f"Google API response: {data}")

            if data.get('status') == 'OK' and data['rows']:
                elements = data['rows'][0]['elements'][0]
                if elements['status'] == 'OK':
                    distance = elements['distance']['value']  # in meters
                    duration = elements['duration']['text']   # human-readable
                    if distance < min_distance:
                        min_distance = distance
                        nearest_ambulance = ambulance
                        nearest_ambulance_time = duration
                else:
                    app.logger.warning(f"Google API returned invalid distance for ambulance {ambulance['id']}")
            else:
                error_msg = data.get('error_message', 'No valid distance available')
                app.logger.error(f"Error from Google Distance Matrix API: {error_msg}")

        if nearest_ambulance:
            # Update ambulance status to 'busy'
            ambulance_id = nearest_ambulance['id']
            db.collection('ambulances').document(ambulance_id).update({
                'status': 'busy'
            })
            app.logger.debug(f"Nearest ambulance: {nearest_ambulance}")

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

if __name__ == '__main__':
    # 3. Read PORT from environment (Railway sets it automatically)
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
