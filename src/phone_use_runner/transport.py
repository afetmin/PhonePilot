import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOL = "phone-bridge-sas-v1"


class BridgeError(RuntimeError):
    def __init__(self, code, message, *, action_executed=None):
        super().__init__(message)
        self.value = {"error": {"code": code, "message": message}, "action_executed": action_executed}


def digest(purpose, *values):
    return hashlib.sha256("\0".join((PROTOCOL, purpose, *values)).encode()).hexdigest()


def normalize_address(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Use the phone's https://host:8443 address")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"https://{host}:{parsed.port or 8443}"


class PhoneTransport:
    """Pinned TLS before credentials, with one reusable HTTPS connection per phone."""

    def __init__(self, config: Path):
        self.config = config
        self.guard = threading.RLock()
        self.locks: dict[str, threading.RLock] = {}
        self.connections: dict[str, tuple[str, str | None, http.client.HTTPSConnection]] = {}

    def _load(self):
        if not self.config.exists():
            return {"protocol": PROTOCOL, "devices": {}}
        value = json.loads(self.config.read_text(encoding="utf-8"))
        if value.get("protocol") != PROTOCOL:
            raise ValueError("Choose a separate PHONE_USE_CONFIG for this phone protocol")
        return value

    def _save(self, value):
        self.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.config.with_name(self.config.name + "." + uuid.uuid4().hex + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.config)

    def _lock(self, address):
        with self.guard:
            return self.locks.setdefault(address, threading.RLock())

    def _request(self, address, path, payload=None, *, pin=None, token=None):
        read_only = (
            path == "/identity" and payload is None
            or path == "/api" and isinstance(payload, dict)
            and payload.get("tool") in {"observe", "get_status", "get_capabilities", "list_apps", "wait_for_change"}
        )
        try:
            return self._request_once(address, path, payload, pin=pin, token=token)
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ssl.SSLEOFError):
            if not read_only:
                raise
            # A phone may close an idle keep-alive socket. Only reads can reconnect;
            # input outcomes remain with the original request and are never replayed here.
            return self._request_once(address, path, payload, pin=pin, token=token)

    def _request_once(self, address, path, payload=None, *, pin=None, token=None):
        address = normalize_address(address)
        if token and not pin:
            raise BridgeError("TLS_NOT_PAIRED", "Pair before sending credentials", action_executed=False)
        with self._lock(address):
            cached = self.connections.get(address)
            if cached and cached[0:2] != (address, pin):
                cached[2].close()
                self.connections.pop(address)
                cached = None
            if not cached:
                url = urlsplit(address)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                connection = http.client.HTTPSConnection(url.hostname, url.port or 8443, context=context, timeout=8)
                self.connections[address] = (address, pin, connection)
            else:
                connection = cached[2]
            try:
                if connection.sock is None:
                    connection.connect()
                actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                if pin and not hmac.compare_digest(pin, actual):
                    raise BridgeError("TLS_IDENTITY_CHANGED", "Phone certificate changed; no credentials sent", action_executed=False)
                headers = {"Accept": "application/json"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                body = None
                if payload is not None:
                    body = json.dumps(payload, ensure_ascii=False).encode()
                    headers["Content-Type"] = "application/json; charset=utf-8"
                connection.request("POST" if body is not None else "GET", path, body, headers)
                response = connection.getresponse()
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise BridgeError("RESPONSE_TOO_LARGE", "Phone response exceeded the transport limit")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise BridgeError("INVALID_RESPONSE", "Expected an object from the phone")
                if response.status != 200:
                    error = value.get("error") or {}
                    if not isinstance(error, dict):
                        error = {}
                    raise BridgeError(error.get("code", "HTTP_ERROR"), f"Phone returned HTTP {response.status}",
                                      action_executed=False if response.status in (400, 401, 403, 404) else None)
                value["_observed_pin"] = actual
                return value
            except Exception:
                connection.close()
                self.connections.pop(address, None)
                raise

    def close(self):
        for _, _, connection in list(self.connections.values()):
            connection.close()
        self.connections.clear()

    def connect(self, address=None, device_id=None, confirm_pairing=False):
        with self.guard:
            config = self._load()
            saved = config["devices"].get(device_id, {}) if device_id else {}
            address = normalize_address(address or saved.get("address", ""))
            identity = self._request(address, "/identity", pin=saved.get("pin"))
            pin = identity.pop("_observed_pin")
            if identity.get("protocol") != PROTOCOL:
                raise ValueError("Install this project's Phone Bridge APK; the original PhoneUse protocol differs")
            actual_id = identity["device_id"]
            if device_id and actual_id != device_id:
                raise ValueError("This address is a different phone installation")
            saved = config["devices"].get(actual_id, saved)
            if saved.get("pin") and not hmac.compare_digest(saved["pin"], pin):
                raise ValueError("Phone certificate changed; verify the installation before pairing again")
            if saved.get("token"):
                try:
                    self._request(address, "/api", {"tool": "get_capabilities", "arguments": {"device_id": actual_id}},
                                  pin=pin, token=saved["token"])
                    return {"status": "paired", "device_id": actual_id, "identity": identity}
                except BridgeError as error:
                    if error.value["error"]["code"] != "NOT_PAIRED":
                        raise
                    saved.pop("token")
                    config["devices"][actual_id] = saved
                    self._save(config)
            pending = saved.get("pending")
            if confirm_pairing:
                if not pending or pending["pin"] != pin:
                    raise ValueError("Begin pairing and compare its code before confirming")
                status = self._request(address, "/pair/status", {"pair_id": pending["pair_id"]}, pin=pin)
                if status.get("status") != "approved":
                    return {"status": status.get("status"), "device_id": actual_id,
                            "message": "Approve on the phone after comparing all digits"}
                config["devices"][actual_id] = {
                    "address": address, "pin": pin, "token": status["token"], "identity": identity,
                }
                self._save(config)
                return {"status": "paired", "device_id": actual_id, "identity": identity}
            nonce = secrets.token_hex(32)
            first = self._request(address, "/pair/request", {
                "client_commit": digest("client-commit", pin, nonce), "client_name": "Phone Use Runner",
            }, pin=pin)
            revealed = self._request(address, "/pair/reveal", {
                "pair_id": first["pair_id"], "client_nonce": nonce,
            }, pin=pin)
            if digest("server-commit", pin, revealed["server_nonce"]) != first["server_commit"]:
                raise ValueError("Pairing commitment mismatch")
            raw = bytes.fromhex(digest("sas", pin, nonce, revealed["server_nonce"]))
            code = f"{int.from_bytes(raw[:4], 'big') % 100_000_000:08d}"
            config["devices"][actual_id] = {"address": address, "pin": pin, "identity": identity,
                                          "pending": {"pair_id": first["pair_id"], "pin": pin}}
            self._save(config)
            return {"status": "pending", "device_id": actual_id, "verification_code": code,
                    "message": "Compare ALL eight digits with the phone, approve there, then explicitly confirm"}

    def devices(self):
        devices = []
        for device_id, config in self._load()["devices"].items():
            item = {"device_id": device_id, "address": config["address"], "paired": bool(config.get("token"))}
            if not item["paired"]:
                item["online"] = False
            else:
                try:
                    identity = self._request(config["address"], "/identity", pin=config["pin"])
                    identity.pop("_observed_pin", None)
                    item.update(identity, online=identity.get("device_id") == device_id)
                except Exception as error:
                    item.update(online=False, error_type=type(error).__name__)
            devices.append(item)
        return {"devices": devices}

    def call(self, device_id: str, tool: str, arguments: dict | None = None) -> dict:
        config = self._load()["devices"].get(device_id)
        if not config or not config.get("token"):
            raise BridgeError("NOT_PAIRED", "Pair this device first", action_executed=False)
        value = self._request(config["address"], "/api", {
            "tool": tool, "arguments": {**(arguments or {}), "device_id": device_id},
        }, pin=config["pin"], token=config["token"])
        value.pop("_observed_pin", None)
        if value.get("device_id") != device_id:
            raise BridgeError("DEVICE_MISMATCH", "Response belongs to another phone installation")
        return value
