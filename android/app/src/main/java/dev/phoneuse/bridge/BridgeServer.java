package dev.phoneuse.bridge;

import android.content.SharedPreferences;
import android.os.Build;
import android.os.SystemClock;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.math.BigInteger;
import java.net.Socket;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.Principal;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.Date;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import javax.net.ssl.KeyManager;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLEngine;
import javax.net.ssl.X509ExtendedKeyManager;
import javax.net.ssl.X509KeyManager;
import javax.security.auth.x500.X500Principal;
import org.json.JSONObject;
import fi.iki.elonen.NanoHTTPD;

final class BridgeServer extends NanoHTTPD {
    static final String PROTOCOL = "phone-bridge-sas-v1";
    static final String TLS_ALIAS = "phone-bridge-tls-v2";
    final BridgeService service;
    final SharedPreferences preferences;
    final String deviceId;
    final String pin;
    private Pair pending;
    private long lastPair;

    static final class Pair {
        String id, clientCommit, serverNonce, clientNonce, code, token;
        long expires;
        boolean approved, denied;
    }

    BridgeServer(BridgeService service) throws Exception {
        super(8443);
        this.service = service;
        preferences = service.getSharedPreferences("bridge", 0);
        deviceId = preferences.getString("device_id", UUID.randomUUID().toString());
        preferences.edit().putString("device_id", deviceId).apply();
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(TLS_ALIAS)) {
            KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA", "AndroidKeyStore");
            generator.initialize(new KeyGenParameterSpec.Builder(TLS_ALIAS,
                    KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                .setKeySize(2048)
                .setDigests(KeyProperties.DIGEST_NONE, KeyProperties.DIGEST_SHA256,
                    KeyProperties.DIGEST_SHA384, KeyProperties.DIGEST_SHA512)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setSignaturePaddings(KeyProperties.SIGNATURE_PADDING_RSA_PKCS1, KeyProperties.SIGNATURE_PADDING_RSA_PSS)
                .setCertificateSubject(new X500Principal("CN=Phone Bridge"))
                .setCertificateSerialNumber(BigInteger.ONE)
                .setCertificateNotBefore(new Date(0))
                .setCertificateNotAfter(new Date(4102444800000L)).build());
            generator.generateKeyPair();
        }
        pin = hash(store.getCertificate(TLS_ALIAS).getEncoded());
        KeyManagerFactory keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        keys.init(store, null);
        X509KeyManager delegate = null;
        for (KeyManager manager : keys.getKeyManagers()) {
            if (manager instanceof X509KeyManager) {
                delegate = (X509KeyManager) manager;
                break;
            }
        }
        if (delegate == null) throw new IllegalStateException("AndroidKeyStore X509 key manager unavailable");
        PrivateKey privateKey = delegate.getPrivateKey(TLS_ALIAS);
        X509Certificate[] certificateChain = delegate.getCertificateChain(TLS_ALIAS);
        if (privateKey == null || certificateChain == null || certificateChain.length == 0 ||
            !"RSA".equalsIgnoreCase(privateKey.getAlgorithm()) ||
            certificateChain[0] == null ||
            !"RSA".equalsIgnoreCase(certificateChain[0].getPublicKey().getAlgorithm()))
            throw new IllegalStateException("AndroidKeyStore TLS key is unavailable or not RSA");
        X509ExtendedKeyManager fixedKeys = new FixedTlsKeyManager(delegate, TLS_ALIAS);
        SSLContext tls = SSLContext.getInstance("TLS");
        tls.init(new KeyManager[]{fixedKeys}, null, new SecureRandom());
        makeSecure(tls.getServerSocketFactory(), new String[]{"TLSv1.2", "TLSv1.3"});
    }

    private static final class FixedTlsKeyManager extends X509ExtendedKeyManager {
        private final X509KeyManager delegate;
        private final String alias;

        FixedTlsKeyManager(X509KeyManager delegate, String alias) {
            this.delegate = delegate;
            this.alias = alias;
        }

        @Override public String[] getClientAliases(String keyType, Principal[] issuers) { return null; }

        @Override public String chooseClientAlias(String[] keyTypes, Principal[] issuers, Socket socket) {
            return null;
        }

        @Override public String[] getServerAliases(String keyType, Principal[] issuers) {
            if (!isRsaKeyType(keyType)) return null;
            String[] aliases = delegate.getServerAliases(keyType, issuers);
            if (aliases != null) {
                for (String candidate : aliases) {
                    if (alias.equals(candidate)) return new String[]{alias};
                }
            }
            return null;
        }

        @Override public String chooseServerAlias(String keyType, Principal[] issuers, Socket socket) {
            return isRsaKeyType(keyType) ? alias : null;
        }

        @Override public PrivateKey getPrivateKey(String requestedAlias) {
            return alias.equals(requestedAlias) ? delegate.getPrivateKey(alias) : null;
        }

        @Override public X509Certificate[] getCertificateChain(String requestedAlias) {
            return alias.equals(requestedAlias) ? delegate.getCertificateChain(alias) : null;
        }

        @Override public String chooseEngineClientAlias(String[] keyTypes, Principal[] issuers, SSLEngine engine) {
            return null;
        }

        @Override public String chooseEngineServerAlias(String keyType, Principal[] issuers, SSLEngine engine) {
            return isRsaKeyType(keyType) ? alias : null;
        }

        private static boolean isRsaKeyType(String keyType) {
            return "RSA".equalsIgnoreCase(keyType);
        }
    }

    static String hash(byte[] value) throws Exception {
        return hex(MessageDigest.getInstance("SHA-256").digest(value));
    }

    private static String hex(byte[] bytes) {
        char[] alphabet = "0123456789abcdef".toCharArray();
        char[] out = new char[bytes.length * 2];
        for (int index = 0; index < bytes.length; index++) {
            out[index * 2] = alphabet[(bytes[index] & 255) >>> 4];
            out[index * 2 + 1] = alphabet[bytes[index] & 15];
        }
        return new String(out);
    }

    static String digest(String purpose, String... values) throws Exception {
        return hash((PROTOCOL + "\0" + purpose + "\0" + String.join("\0", values)).getBytes("UTF-8"));
    }

    static String randomHex() {
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        return hex(bytes);
    }

    JSONObject identity() throws Exception {
        return new JSONObject().put("protocol", PROTOCOL).put("device_id", deviceId)
            .put("service_instance_id", service.instanceId).put("name", Build.MODEL)
            .put("android_version", Build.VERSION.RELEASE).put("app_version", "0.1.0");
    }

    synchronized String pairingCode() {
        return pending != null && pending.expires > SystemClock.elapsedRealtime()
            && !pending.denied && !pending.approved ? pending.code : null;
    }

    synchronized void approve() {
        if (pairingCode() == null) return;
        try {
            pending.token = randomHex();
            preferences.edit().putString("token_hash", hash(pending.token.getBytes("UTF-8"))).commit();
            pending.approved = true;
        } catch (Exception error) { pending.denied = true; }
    }

    synchronized void deny() { if (pending != null) pending.denied = true; }

    synchronized void revoke() {
        preferences.edit().remove("token_hash").commit();
        pending = null;
        service.resetGeneration();
    }

    private synchronized JSONObject pair(String path, JSONObject body) throws Exception {
        long now = SystemClock.elapsedRealtime();
        if (path.equals("/pair/request")) {
            if (now - lastPair < 5000 && lastPair != 0) throw new IllegalArgumentException("Pairing rate limited");
            if (pending != null && pending.expires > now && !pending.denied && !pending.approved)
                throw new IllegalArgumentException("A local pairing request is already pending");
            String commitment = body.getString("client_commit");
            if (!commitment.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("Invalid commitment");
            pending = new Pair();
            pending.id = randomHex(); pending.clientCommit = commitment; pending.serverNonce = randomHex();
            pending.expires = now + 120000; lastPair = now;
            return new JSONObject().put("pair_id", pending.id)
                .put("server_commit", digest("server-commit", pin, pending.serverNonce));
        }
        if (pending == null || pending.expires < now || !pending.id.equals(body.optString("pair_id")))
            return new JSONObject().put("status", "expired");
        if (path.equals("/pair/reveal")) {
            String nonce = body.getString("client_nonce");
            if (!nonce.matches("[0-9a-f]{64}") ||
                !pending.clientCommit.equals(digest("client-commit", pin, nonce))) {
                pending.denied = true;
                throw new IllegalArgumentException("Commitment mismatch");
            }
            pending.clientNonce = nonce;
            String hex = digest("sas", pin, nonce, pending.serverNonce);
            pending.code = String.format(Locale.ROOT, "%08d", Long.parseLong(hex.substring(0, 8), 16) % 100000000);
            return new JSONObject().put("server_nonce", pending.serverNonce);
        }
        if (path.equals("/pair/status")) {
            JSONObject result = new JSONObject().put("status", pending.denied ? "denied" : pending.approved ? "approved" : "pending");
            if (pending.approved) result.put("token", pending.token);
            return result;
        }
        throw new IllegalArgumentException("Unknown pairing endpoint");
    }

    private Response response(Response.Status status, JSONObject result) {
        Response response = newFixedLengthResponse(status, "application/json; charset=utf-8", result.toString());
        response.addHeader("Cache-Control", "no-store");
        return response;
    }

    @Override public Response serve(IHTTPSession session) {
        try {
            if (session.getHeaders().containsKey("origin"))
                return response(Response.Status.FORBIDDEN, new JSONObject().put("error", "Native clients only"));
            String path = session.getUri();
            if (path.equals("/identity") && session.getMethod() == Method.GET)
                return response(Response.Status.OK, identity());
            if (session.getMethod() != Method.POST || (!path.equals("/api") && !path.startsWith("/pair/")))
                return response(Response.Status.NOT_FOUND, new JSONObject());
            if (path.equals("/api")) {
                String authorization = session.getHeaders().getOrDefault("authorization", "");
                String saved = preferences.getString("token_hash", "");
                if (!authorization.startsWith("Bearer ") || saved.isEmpty() ||
                    !MessageDigest.isEqual(saved.getBytes("UTF-8"), hash(authorization.substring(7).getBytes("UTF-8")).getBytes("UTF-8")))
                    return response(Response.Status.UNAUTHORIZED, new JSONObject().put("error", new JSONObject().put("code", "NOT_PAIRED")));
            }
            int size = Integer.parseInt(session.getHeaders().getOrDefault("content-length", "0"));
            if (size < 2 || size > 65536) throw new IllegalArgumentException("Invalid request size");
            Map<String, String> files = new HashMap<>();
            session.parseBody(files);
            JSONObject body = new JSONObject(files.getOrDefault("postData", "{}"));
            if (path.startsWith("/pair/")) return response(Response.Status.OK, pair(path, body));
            JSONObject arguments = body.optJSONObject("arguments");
            if (arguments == null) arguments = new JSONObject();
            JSONObject result;
            if (!deviceId.equals(arguments.optString("device_id"))) {
                result = BridgeService.failure("DEVICE_MISMATCH", false);
            } else if (arguments.has("service_instance_id") && !service.instanceId.equals(arguments.getString("service_instance_id"))) {
                result = BridgeService.failure("STALE_OBSERVATION", false);
            } else {
                result = service.call(body.getString("tool"), arguments);
            }
            result.put("device_id", deviceId).put("service_instance_id", service.instanceId);
            return response(Response.Status.OK, result);
        } catch (Exception error) {
            return response(Response.Status.BAD_REQUEST, new JSONObject());
        }
    }
}
