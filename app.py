from flask import Flask, request, jsonify, send_from_directory 
from models import db, PCBProfile, TestResult
import json
import pyvisa
import os
import time
from datetime import datetime
import csv
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from libretranslatepy import LibreTranslateAPI
import base64
import io
from PIL import Image

lt = LibreTranslateAPI("http://localhost:5000")

# DMM IP address
KEITHLEY_IP = "10.10.1.162"
app = Flask(__name__, static_folder='../frontend', static_url_path='')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..',
    'database',
    'pcb_tester.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)


@app.route('/api/get-ip', methods=['GET'])
def get_instrument_ip():
    try:
        dmm_ip = KEITHLEY_IP  # Replace with your actual way of determining IP
        return jsonify({"ip": dmm_ip})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/api/get-idn', methods=['GET'])
def get_idn():
    try:
        dmm, rm = connect_to_dmm(KEITHLEY_IP)  # Use your IP logic
        idn = dmm.query("*IDN?").strip()
        dmm.close()
        rm.close()
        return jsonify({"idn": idn})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def connect_to_dmm(ip):
    try:
        rm = pyvisa.ResourceManager()  # Use NI-VISA
        resource = f"TCPIP0::{ip}::inst0::INSTR"
        inst = rm.open_resource(resource)
        inst.timeout = 10000
        inst.write("*RST")
        inst.write("*CLS")
        idn = inst.query("*IDN?").strip()
        print("[DEBUG] Connected to:", idn)
        return inst, rm
    except Exception as e:
        print(f"[ERROR] Could not connect to DMM: {e}")
        raise
        
@app.route('/api/save-profile', methods=['POST'])
def save_profile():
    try:
        data = request.json

        if not data.get('name'):
            return jsonify({"status": "error", "error": "Profile name is required"}), 400

        if data.get('id'):
            # Update existing profile
            profile = PCBProfile.query.get(data['id'])
            if not profile:
                return jsonify({"status": "error", "error": "Profile not found"}), 404
            profile.name = data['name']
            profile.remarks = data.get('remarks', '')
            profile.tests = json.dumps(data['steps'])
        else:
            # Create new profile
            profile = PCBProfile(
                name=data['name'],
                remarks=data.get('remarks', ''),
                tests=json.dumps(data['steps'])
            )
            db.session.add(profile)

        db.session.commit()
        return jsonify({"status": "success"})

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/api/profiles', methods=['GET'])
def get_profiles():
    profiles = PCBProfile.query.all()
    profiles_list = []
    for p in profiles:
        try:
            tests = json.loads(p.tests) if p.tests else []
        except json.JSONDecodeError:
            tests = []
            
        profiles_list.append({
            "id": p.id,
            "name": p.name,
            "remarks": p.remarks or '',
            "tests": tests
        })
    return jsonify(profiles_list)

def create_keithley_graph(dmm, measurement_data, measurement_type, channel):
    """Create a graph on Keithley DMM6500 using SCPI commands and capture it"""
    try:
        # First, clear any existing user measurements
        dmm.write("CALCulate:PARameter:DELete:ALL")
        
        # Create a user-defined measurement
        dmm.write("CALCulate:MEASure:DEFine 'USER1', 'User Display'")
        
        # Set X-axis values (time points for our measurements)
        # We'll use sequential time points based on the number of measurements
        num_points = len(measurement_data)
        x_values = ",".join(str(i) for i in range(1, num_points + 1))
        dmm.write(f"SENSe:USER:X:VALues {x_values}")
        
        # Set X-axis properties
        dmm.write("SENSe:USER:X:AXIS:UNIT 's'")  # Seconds as unit
        dmm.write("SENSe:USER:X:AXIS:NAME 'Time'")  # Time as axis name
        
        # Set Y-axis properties based on measurement type
        unit_map = {
            'DC_VOLTAGE': 'V',
            'AC_VOLTAGE': 'V', 
            'DC_CURRENT': 'A',
            'AC_CURRENT': 'A',
            'RESISTANCE': 'Ω',
            'CAPACITANCE': 'F',
            'FREQUENCY': 'Hz',
            'TEMPERATURE': '°C'
        }
        unit = unit_map.get(measurement_type, '')
        dmm.write(f"CALCulate:MEASure:Y:AXIS:UNIT:CUSTom ON")
        dmm.write(f"CALCulate:MEASure:Y:AXIS:UNIT:CUSTom:DATA '{unit}'")
        
        # Send the measurement data to the instrument
        # Format: real1,imag1,real2,imag2,... (we'll use 0 for imaginary parts)
        sdat_values = []
        for value in measurement_data:
            sdat_values.append(f"{value},0")
        sdat_str = ",".join(sdat_values)
        dmm.write(f"CALCulate:MEASure:DATA:SDAT {sdat_str}")
        
        # Display the measurement in a window
        dmm.write("DISPlay:WINDow:STATE ON")
        dmm.write("DISPlay:MEASure:FEED 1")  # Display in window 1
        
        # Set trace title
        channel_label = f"Ch{channel}" if channel else "Front"
        dmm.write(f"DISPlay:WINDow:TRACe1:TITLe:DATA '{measurement_type} ({channel_label})'")
        dmm.write("DISPlay:WINDow:TRACe1:TITLe:STATE ON")
        
        # Set autoscale for the display
        dmm.write("DISPlay:WINDow:Y:AUTO")
        
        # Wait for display to update
        time.sleep(1.5)
        
        # Now capture the display using hardcopy
        dmm.write("HCOP:DEV:LANG PNG")
        dmm.write("HCOP:COLor:STATE ON")
        dmm.write("HCOP:DEST 'NETWORK'")
        dmm.write("HCOP:IMM")
        
        # Wait for capture to complete
        time.sleep(1)
        
        # Read the image data
        dmm.write("HCOP:DATA?")
        image_data = dmm.read_raw()
        
        # Process the image data
        if image_data and image_data.startswith(b'#'):
            num_length_digits = int(chr(image_data[1]))
            length_str = image_data[2:2+num_length_digits].decode('ascii')
            data_length = int(length_str)
            
            header_length = 2 + num_length_digits
            img_data = image_data[header_length:header_length + data_length]
            
            if len(img_data) == data_length:
                img_base64 = base64.b64encode(img_data).decode('utf-8')
                return img_base64
        
        return None
        
    except Exception as e:
        print(f"[ERROR] Failed to create graph: {e}")
        # Try to get error information
        try:
            error_msg = dmm.query("SYST:ERR?")
            print(f"[ERROR] Instrument error: {error_msg}")
        except:
            pass
        return None


@app.route('/api/run-test', methods=['POST']) 
def run_test():
    results = []
    graphs = []  # Store graphs for each step
    measurement_data = {}  # Store measurement values for each step
    dmm = None
    rm = None
    
    try:
        data = request.get_json()
        ip = data.get('ip', KEITHLEY_IP)
        is_quick_test = data.get('isQuickTest', False)
        capture_graphs = data.get('captureGraphs', False)
        profile_name = data.get('profileName', 'Quick Test' if is_quick_test else 'Unnamed Profile')

        # Connect to instrument
        dmm, rm = connect_to_dmm(ip)

        # Validate all channels before executing any tests
        for step in data.get('steps', []):
            if step.get('source', 'REAR').upper() == 'REAR':
                channel = step.get('channel')
                if channel and int(channel) > 10:
                    raise ValueError(f"Invalid channel {channel}. Only channels 1-10 are supported.")

        # Only open all relays if using REAR channels
        if any(step.get('source', 'REAR').upper() != 'FRONT' for step in data.get('steps', [])):
            dmm.write("ROUT:OPEN:ALL")
            time.sleep(0.5)  # Allow relays time to open

        # Process each test step
        for step_index, step in enumerate(data.get('steps', [])):
            step_measurements = []  # Store measurements for this step
            try:
                source = step.get('source', 'REAR').upper()
                channel = step.get('channel', '')
                measure_type = step.get('type', 'DC_VOLTAGE').upper()
                unit = {
                    'DC_VOLTAGE': 'V',
                    'AC_VOLTAGE': 'V',
                    'DC_CURRENT': 'A',
                    'AC_CURRENT': 'A',
                    'RESISTANCE': 'Ω',
                    'CAPACITANCE': 'F',
                    'CONTINUITY': 'Ω',
                    'FREQUENCY': 'Hz',
                    'TEMPERATURE': '°C'
                }.get(measure_type, '')

                # Configure channel if using rear panel
                if source == 'REAR':
                    dmm.write("ROUT:TERM REAR")
                    if channel:
                        dmm.write(f"ROUT:CLOS (@{channel})")
                        time.sleep(0.2)  # Allow relay to settle

                # Configure measurement type
                cmd_map = {
                    'DC_VOLTAGE': 'MEAS:VOLT:DC?',
                    'AC_VOLTAGE': 'MEAS:VOLT:AC?',
                    'DC_CURRENT': 'MEAS:CURR:DC?',
                    'AC_CURRENT': 'MEAS:CURR:AC?',
                    'RESISTANCE': 'MEAS:RES?',
                    'CAPACITANCE': 'MEAS:CAP?',
                    'CONTINUITY': 'MEAS:CONT?',
                    'FREQUENCY': 'MEAS:FREQ?',
                    'TEMPERATURE': 'MEAS:TEMP?'
                }
                cmd = cmd_map.get(measure_type, 'MEAS:VOLT:DC?')
                
                # Special handling for Continuity test
                if measure_type == 'CONTINUITY':
                    dmm.write("SENS:FUNC 'CONT'")
                    time.sleep(0.3)
                    raw_value = dmm.query("READ?")
                    value = float(raw_value)
                    threshold = 10  # Ω
                    passed = value <= threshold
                    target_value = f"≤{threshold}Ω"
                    dmm.write("FUNC 'VOLT:DC'")  # Return to default
                    step_measurements.append(value)
                else:
                    # Normal measurement handling
                    if step.get('useMean'):
                        count = int(step.get('measurements', 3))
                        delay = float(step.get('delay', 0.5))
                        vals = []
                        for _ in range(count):
                            val = float(dmm.query(cmd).strip())
                            vals.append(val)
                            step_measurements.append(val)
                            time.sleep(delay)
                        value = sum(vals) / len(vals)
                    else:
                        value = float(dmm.query(cmd).strip())
                        step_measurements.append(value)

                    # Pass/fail logic
                    if step.get('useRange'):
                        mn = float(step.get('min', 0))
                        mx = float(step.get('max', 0))
                        passed = mn <= value <= mx
                        target_value = f"{mn}-{mx}{unit}"
                    else:
                        target = float(step.get('target', 0))
                        tol = float(step.get('tolerance', 0.1))
                        passed = (target - tol) <= value <= (target + tol)
                        target_value = f"{target}±{tol}{unit}"

                # Store measurement data for potential graph creation
                measurement_data[step_index] = step_measurements

                # Capture graph if requested and we have multiple measurements
                graph_data = None
                if (capture_graphs and len(step_measurements) > 1 and 
                    measure_type not in ['CONTINUITY']):  # Skip continuity tests
                    try:
                        graph_data = create_keithley_graph(
                            dmm, step_measurements, measure_type, channel
                        )
                    except Exception as graph_err:
                        print(f"[WARNING] Failed to create graph for step {step_index}: {graph_err}")
                        graph_data = None

                # Store result
                results.append({
                    "source": source,
                    "channel": channel,
                    "type": measure_type,
                    "value": value,
                    "target": target_value,
                    "passed": passed,
                    "timestamp": time.time(),
                    "unit": unit
                })
                
                # Store graph if captured
                if graph_data:
                    graphs.append({
                        "step_index": step_index,
                        "graph": graph_data,
                        "type": measure_type,
                        "channel": channel if source == 'REAR' else 'FRONT'
                    })

                # Open channel if used
                if source == 'REAR' and channel:
                    dmm.write(f"ROUT:OPEN (@{channel})")

            except Exception as st_err:
                print(f"[ERROR] Step error: {st_err}")
                err_type = step.get('type', 'UNKNOWN')
                err_unit = unit_map.get(err_type, '')
                
                results.append({
                    "source": step.get('source', 'REAR'),
                    "channel": step.get('channel', ''),
                    "type": err_type,
                    "error": str(st_err),
                    "passed": False,
                    "timestamp": time.time(),
                    "unit": err_unit,
                    "target": "N/A"
                })

        # Save results to database (unless quick test)
        if not is_quick_test:
            test_rec = TestResult(
                profile_name=profile_name,
                results=json.dumps(results),
                graphs=json.dumps(graphs) if graphs else None,
                timestamp=datetime.now()
            )
            db.session.add(test_rec)
            db.session.commit()

        return jsonify({
            "status": "success",
            "results": results,
            "graphs": graphs,  # Include graphs in response
            "idn": dmm.query("*IDN?").strip() if dmm else "Unknown",
            "isQuickTest": is_quick_test
        })

    except Exception as err:
        print(f"[ERROR] run_test outer failure: {err}")
        if db.session:
            db.session.rollback()
        return jsonify({
            "status": "failed", 
            "error": str(err),
            "results": results,
            "isQuickTest": data.get('isQuickTest', False) if 'data' in locals() else False
        }), 500

    finally:
        # Clean up instrument connection
        try:
            if dmm:
                if any(r.get('source', 'REAR') != 'FRONT' for r in results):
                    dmm.write("ROUT:OPEN:ALL")
                dmm.close()
            if rm:
                rm.close()
        except Exception as cleanup_err:
            print(f"[WARNING] Cleanup error: {cleanup_err}")

@app.route('/results/<path:filename>')
def download_results(filename):
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frontend', 'results')
    return send_from_directory(folder, filename)

@app.route('/api/delete-profile/<int:profile_id>', methods=['DELETE'])
def delete_profile(profile_id):
    try:
        profile = PCBProfile.query.get(profile_id)
        if not profile:
            return jsonify({"status": "error", "error": "Profile not found"}), 404
        db.session.delete(profile)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/')
def serve_frontend():
    return send_from_directory('../frontend', 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory('../frontend', filename)

@app.route('/debug')
def debug():
    return f"Results folder: {os.path.abspath(os.path.join('..','frontend','results'))}"

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)
