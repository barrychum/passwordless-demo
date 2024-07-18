from typing import Dict, Optional, List
from dataclasses import dataclass, field
import json
import os

from flask import Flask, render_template, request, jsonify, redirect, url_for
from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    RegistrationCredential,
    AuthenticationCredential,
    AuthenticatorTransport,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier

# Data models
@dataclass
class Credential:
    id: bytes
    public_key: bytes
    sign_count: int
    transports: Optional[List[AuthenticatorTransport]] = None

@dataclass
class UserAccount:
    username: str
    credentials: List[Credential] = field(default_factory=list)

@dataclass
class SessionVar:
    username: str
    expected_challenge: str

# In-memory storage
in_memory_db: Dict[str, UserAccount] = {}
in_memory_session: Dict[str, SessionVar] = {}

app = Flask(__name__)

# Relying Party Configuration
RP_ID = "localhost"
RP_NAME = "Sample Relying Party"
ORIGIN = f"http://{RP_ID}:5000"  # Default, to be modified in root()

def update_session(username: str, challenge: str) -> None:
    """Update or create a session for a user with a new challenge."""
    in_memory_session[username] = SessionVar(username=username, expected_challenge=challenge)

@app.route('/')
def root():
    """Render the main page and update global configuration."""
    global RP_ID, ORIGIN
    
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
    host = request.headers.get('X-Forwarded-Host', request.headers.get('Host'))
    host_without_port = host.split(':')[0]
    RP_ID = host_without_port
    ORIGIN = f"{scheme}://{host}"

    return render_template('index.html', rp_id=RP_ID, rp_name=RP_NAME, origin=ORIGIN)

@app.route('/sys', methods=['GET', 'POST'])
def sys():
    """Handle system configuration updates."""
    global RP_ID, ORIGIN, RP_NAME
    
    if request.method == 'GET':
        context = {
            "rp_id": RP_ID,
            "rp_name": RP_NAME,
            "origin": ORIGIN,
            "user_data": in_memory_db
        }
        return render_template('sys.html', **context)
    else:
        RP_ID = request.form['rp_id']
        ORIGIN = request.form['origin']
        RP_NAME = request.form['rp_name']
        return redirect(url_for('root'))

@app.route('/register', methods=['POST'])
def register():
    """Handle user registration."""
    username = request.form['username']

    if username not in in_memory_db:
        in_memory_db[username] = UserAccount(username=username)
    
    user = in_memory_db[username]

    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user.username,
        user_name=user.username,
        exclude_credentials=[
            {"id": cred.id, "transports": cred.transports, "type": "public-key"}
            for cred in user.credentials
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )

    update_session(user.username, options.challenge)
    return options_to_json(options)

@app.route("/verify-registration", methods=["POST"])
def verify_registration():
    """Verify the registration response from the client."""
    data = request.json
    body = data.get("original_json").encode()
    username = data.get("username")

    try:
        credential = RegistrationCredential.parse_raw(body)
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=in_memory_session[username].expected_challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
        )
    except Exception as err:
        return jsonify(verified=False, msg=str(err)), 400

    new_credential = Credential(
        id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=json.loads(body).get("transports", []),
    )

    in_memory_db[username].credentials.append(new_credential)
    return jsonify(verified=True)

@app.route('/authenticate', methods=['POST'])
def authenticate():
    """Handle user authentication."""
    username = request.form['username']

    if username not in in_memory_db:
        return jsonify(message='User does not exist!'), 404

    user = in_memory_db[username]
    options = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[
            {"type": "public-key", "id": cred.id, "transports": cred.transports}
            for cred in user.credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    update_session(user.username, options.challenge)
    return options_to_json(options)

@app.route('/verify-authentication', methods=['POST'])
def verify_authentication():
    """Verify the authentication response from the client."""
    data = request.json
    body = data.get("original_json").encode()
    username = data.get("username")

    try:
        credential = AuthenticationCredential.parse_raw(body)
        user = in_memory_db[username]
        user_credential = next((cred for cred in user.credentials if cred.id == credential.raw_id), None)

        if not user_credential:
            raise ValueError("Could not find corresponding public key in DB")

        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=in_memory_session[username].expected_challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            credential_public_key=user_credential.public_key,
            credential_current_sign_count=user_credential.sign_count,
            require_user_verification=True,
        )
    except Exception as err:
        return jsonify(verified=False, msg=str(err)), 400

    user_credential.sign_count = verification.new_sign_count
    return jsonify(verified=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))    # railway.app set environment variable of the app
    app.run(host='0.0.0.0', port=port, debug=True)
