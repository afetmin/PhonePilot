package dev.phoneuse.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.KeyguardManager;
import android.content.Intent;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Base64;
import android.view.Display;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;
import org.json.JSONArray;
import org.json.JSONObject;

public final class BridgeService extends AccessibilityService {
    public static volatile BridgeService current;
    volatile BridgeServer server;
    volatile String serverError;
    volatile String instanceId = UUID.randomUUID().toString();
    volatile boolean enabled = true;
    private volatile boolean destroyed;
    final AtomicLong version = new AtomicLong();
    private final Object changes = new Object();
    private final ReentrantLock work = new ReentrantLock();
    private final Map<String, Snapshot> snapshots = new LinkedHashMap<>();
    private final Map<String, Request> requests = new LinkedHashMap<>();
    private static final List<String> ACTIONS = List.of("tap", "long_press", "input_text", "scroll", "swipe", "back", "home", "launch_app");

    static final class Snapshot {
        int window, width, height, rotation;
        String app, generation;
        long created, uiVersion;
        final Map<String, String> nodes = new HashMap<>();
    }

    static final class Request {
        String signature;
        long created = SystemClock.elapsedRealtime();
        volatile boolean cancelled;
        volatile JSONObject result;
    }

    static final class Failure extends RuntimeException {
        final String code;
        final boolean uncertain;
        Failure(String code) { this(code, false); }
        Failure(String code, boolean uncertain) { super(code); this.code = code; this.uncertain = uncertain; }
    }

    static JSONObject failure(String code, boolean uncertain) {
        try {
            return new JSONObject().put("state", uncertain ? "unknown" : "failed")
                .put("action_executed", uncertain ? JSONObject.NULL : false)
                .put("error", new JSONObject().put("code", code));
        } catch (Exception impossible) { throw new IllegalStateException(impossible); }
    }

    @Override protected void onServiceConnected() {
        current = this;
        setControl(getSharedPreferences("bridge", 0).getBoolean("enabled", true));
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        int type = event.getEventType();
        if (type == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED ||
            type == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED ||
            type == AccessibilityEvent.TYPE_VIEW_SCROLLED) {
            AccessibilityNodeInfo active = getRootInActiveWindow();
            if (active != null) {
                try {
                    // Snapshots contain only the active window. Background and status-bar
                    // content updates do not invalidate its nodes or recipient proof.
                    if (event.getWindowId() >= 0 && event.getWindowId() != active.getWindowId()) return;
                } finally { active.recycle(); }
            }
        }
        version.incrementAndGet();
        synchronized (changes) { changes.notifyAll(); }
    }
    @Override public void onInterrupt() { version.incrementAndGet(); }
    @Override public void onDestroy() {
        destroyed = true;
        setControl(false);
        if (current == this) current = null;
        super.onDestroy();
    }

    synchronized void setControl(boolean value) {
        enabled = value;
        if (!value) {
            if (server != null) server.stop();
            server = null;
            resetGeneration();
        } else if (server == null && !destroyed) {
            new Thread(() -> {
                try {
                    BridgeServer next = new BridgeServer(this);
                    synchronized (this) {
                        if (!enabled || destroyed || server != null) return;
                        resetGeneration();
                        // Keep the paired client's HTTP connection open while idle.
                        // The desktop bounds individual RPCs and closes sockets on shutdown.
                        next.start(0, true);
                        server = next; serverError = null;
                    }
                } catch (Exception error) { serverError = error.getClass().getSimpleName(); }
            }, "bridge-start").start();
        }
    }

    void resetGeneration() {
        instanceId = UUID.randomUUID().toString();
        synchronized (changes) { changes.notifyAll(); }
        synchronized (snapshots) { snapshots.clear(); }
        synchronized (requests) { requests.values().forEach(request -> request.cancelled = true); }
    }

    private void ready() {
        if (!enabled) throw new Failure("STOPPED");
        if (!getSystemService(PowerManager.class).isInteractive()) throw new Failure("SCREEN_OFF");
        if (getSystemService(KeyguardManager.class).isKeyguardLocked()) throw new Failure("DEVICE_LOCKED");
    }

    private int[] geometry() {
        WindowManager manager = getSystemService(WindowManager.class);
        Rect bounds = manager.getMaximumWindowMetrics().getBounds();
        return new int[]{bounds.width(), bounds.height(), manager.getDefaultDisplay().getRotation()};
    }

    private String reference(AccessibilityNodeInfo node) throws Exception {
        Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
        return BridgeServer.hash((node.hashCode() + "|" + node.getWindowId() + "|" +
            (Build.VERSION.SDK_INT >= 33 ? node.getUniqueId() : "") + "|" + node.getViewIdResourceName() + "|" +
            node.getClassName() + "|" + node.getText() + "|" + node.getContentDescription() + "|" +
            bounds.flattenToString() + "|" + node.isVisibleToUser() + "|" + node.isEnabled()).getBytes("UTF-8"));
    }

    private void walk(AccessibilityNodeInfo node, String path, int depth, Snapshot snapshot,
                      JSONArray nodes, int[] count, boolean[] complete, long deadline, boolean details) throws Exception {
        if (++count[0] > 1500 || depth > 40) { complete[0] = false; complete[1] = true; return; }
        if (SystemClock.elapsedRealtime() > deadline) throw new Failure("TREE_TIMEOUT");
        Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
        boolean visible = node.isVisibleToUser() && !bounds.isEmpty() &&
            Rect.intersects(bounds, new Rect(0, 0, snapshot.width, snapshot.height));
        JSONArray actions = new JSONArray();
        for (AccessibilityNodeInfo.AccessibilityAction action : node.getActionList()) {
            switch (action.getId()) {
                case AccessibilityNodeInfo.ACTION_CLICK: actions.put("tap"); break;
                case AccessibilityNodeInfo.ACTION_LONG_CLICK: actions.put("long_press"); break;
                case AccessibilityNodeInfo.ACTION_SET_TEXT: if (node.isEditable() && !node.isPassword()) actions.put("set_text"); break;
                case AccessibilityNodeInfo.ACTION_SCROLL_FORWARD: actions.put("scroll_forward"); break;
                case AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD: actions.put("scroll_backward"); break;
            }
        }
        CharSequence text = node.isPassword() ? null : node.isShowingHintText() ? "" : node.getText();
        CharSequence description = node.isPassword() ? null : node.getContentDescription();
        if (visible && (text != null || description != null || node.getHintText() != null || actions.length() > 0)) {
            JSONObject out = new JSONObject().put("element_id", path)
                .put("parent_id", path.contains(".") ? path.substring(0, path.lastIndexOf('.')) : JSONObject.NULL)
                .put("bounds", new JSONArray(List.of(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                .put("actions", actions);
            if (text != null) { out.put("text", text.toString().substring(0, Math.min(1024, text.length()))); if (text.length() > 1024) out.put("text_truncated", true); }
            if (description != null) { out.put("description", description.toString().substring(0, Math.min(1024, description.length()))); if (description.length() > 1024) out.put("description_truncated", true); }
            if (node.getHintText() != null && !node.isPassword()) {
                String hint = node.getHintText().toString(); out.put("hint", hint.substring(0, Math.min(1024, hint.length())));
            }
            if (node.isEditable()) out.put("editable", true);
            if (node.isPassword()) out.put("password", true);
            if (!node.isEnabled()) out.put("enabled", false);
            if (node.isChecked()) out.put("checked", true);
            if (node.isSelected()) out.put("selected", true);
            if (details) out.put("view_id", node.getViewIdResourceName()).put("class", node.getClassName());
            nodes.put(out);
            snapshot.nodes.put(path, reference(node));
        }
        for (int index = 0; index < node.getChildCount(); index++) {
            if (count[0] >= 1500) { complete[0] = false; complete[1] = true; break; }
            AccessibilityNodeInfo child = node.getChild(index);
            if (child == null) { complete[0] = false; continue; }
            try { walk(child, path + "." + index, depth + 1, snapshot, nodes, count, complete, deadline, details); }
            finally { child.recycle(); }
        }
    }

    private JSONObject screenshot() throws Exception {
        CompletableFuture<JSONObject> result = new CompletableFuture<>();
        takeScreenshot(Display.DEFAULT_DISPLAY, Runnable::run, new TakeScreenshotCallback() {
            @Override public void onSuccess(ScreenshotResult shot) {
                Bitmap hardware = null, software = null, scaled = null;
                try {
                    hardware = Bitmap.wrapHardwareBuffer(shot.getHardwareBuffer(), shot.getColorSpace());
                    software = hardware.copy(Bitmap.Config.ARGB_8888, false);
                    double ratio = Math.min(1.0, 1280.0 / Math.max(software.getWidth(), software.getHeight()));
                    scaled = Bitmap.createScaledBitmap(software, Math.max(1, (int)(software.getWidth() * ratio)),
                                                       Math.max(1, (int)(software.getHeight() * ratio)), true);
                    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                    scaled.compress(Bitmap.CompressFormat.JPEG, 80, bytes);
                    result.complete(new JSONObject().put("available", true).put("mime_type", "image/jpeg")
                        .put("width", scaled.getWidth()).put("height", scaled.getHeight())
                        .put("scale_x", software.getWidth() / (double) scaled.getWidth())
                        .put("scale_y", software.getHeight() / (double) scaled.getHeight())
                        .put("offset_x", 0).put("offset_y", 0)
                        .put("data_base64", Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP)));
                } catch (Exception error) { result.complete(failure("SCREENSHOT_CONVERSION_FAILED", false)); }
                finally {
                    if (scaled != null && scaled != software) scaled.recycle();
                    if (software != null) software.recycle();
                    if (hardware != null) hardware.recycle();
                    shot.getHardwareBuffer().close();
                }
            }
            @Override public void onFailure(int code) {
                try { result.complete(new JSONObject().put("available", false).put("android_error", code)); }
                catch (Exception error) { result.complete(new JSONObject()); }
            }
        });
        try { return result.get(3, TimeUnit.SECONDS); }
        catch (Exception error) { return new JSONObject().put("available", false).put("reason", "SCREENSHOT_TIMEOUT"); }
    }

    private boolean localWindowVisible() {
        List<AccessibilityWindowInfo> windows = getWindows();
        try {
            for (AccessibilityWindowInfo window : windows) {
                if (window.getDisplayId() != Display.DEFAULT_DISPLAY) continue;
                AccessibilityNodeInfo root = window.getRoot();
                try {
                    if (root != null && getPackageName().contentEquals(root.getPackageName())) return true;
                    if (root == null && window.getType() == AccessibilityWindowInfo.TYPE_APPLICATION) return true;
                } finally { if (root != null) root.recycle(); }
            }
            return false;
        } finally { for (AccessibilityWindowInfo window : windows) window.recycle(); }
    }

    private JSONObject observe(JSONObject args) throws Exception {
        ready();
        long start = SystemClock.elapsedRealtime();
        String mode = args.optString("mode", "tree");
        if (!List.of("tree", "both", "screenshot").contains(mode)) throw new Failure("INVALID_ARGUMENT");
        int[] dimensions = geometry();
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            AccessibilityWindowInfo window = root.getWindow();
            try {
                if (window != null && window.getDisplayId() != Display.DEFAULT_DISPLAY) {
                    root.recycle(); throw new Failure("DISPLAY_UNSUPPORTED");
                }
            } finally { if (window != null) window.recycle(); }
        }
        boolean localControl = root != null && getPackageName().contentEquals(root.getPackageName());
        Snapshot snapshot = new Snapshot();
        snapshot.created = start; snapshot.generation = instanceId;
        snapshot.uiVersion = version.get();
        snapshot.width = dimensions[0]; snapshot.height = dimensions[1]; snapshot.rotation = dimensions[2];
        snapshot.window = root == null ? -1 : root.getWindowId();
        snapshot.app = root == null || root.getPackageName() == null ? "" : root.getPackageName().toString();
        JSONArray nodes = new JSONArray(); int[] count = {0}; boolean[] complete = {root != null && !localControl, false};
        long deadline = start + Math.max(1, Math.min(3000, args.optInt("timeout_ms", 3000)));
        try { if (root != null && !localControl && !mode.equals("screenshot")) walk(root, "0", 0, snapshot, nodes, count, complete, deadline, args.optBoolean("include_details")); }
        finally { if (root != null) root.recycle(); }
        long treeFinished = SystemClock.elapsedRealtime();
        JSONObject shot = mode.equals("tree") ? null : localControl || localWindowVisible()
            ? new JSONObject().put("available", false).put("reason", "LOCAL_CONTROL_UI") : screenshot();
        AccessibilityNodeInfo after = getRootInActiveWindow();
        try {
            if ((after == null ? -1 : after.getWindowId()) != snapshot.window ||
                !snapshot.generation.equals(instanceId)) throw new Failure("STALE_OBSERVATION");
        } finally { if (after != null) after.recycle(); }
        Map<String, JSONObject> exported = new HashMap<>();
        for (int index = 0; index < nodes.length(); index++) exported.put(nodes.getJSONObject(index).getString("element_id"), nodes.getJSONObject(index));
        for (int index = 0; index < nodes.length(); index++) {
            JSONObject node = nodes.getJSONObject(index); String parent = node.optString("parent_id", "");
            while (!parent.isEmpty() && !exported.containsKey(parent)) parent = parent.contains(".") ? parent.substring(0, parent.lastIndexOf('.')) : "";
            node.put("parent_id", parent.isEmpty() ? JSONObject.NULL : parent);
        }
        String id = UUID.randomUUID().toString();
        synchronized (snapshots) {
            snapshots.values().removeIf(item -> start - item.created > 30000);
            while (snapshots.size() >= 8) snapshots.remove(snapshots.keySet().iterator().next());
            snapshots.put(id, snapshot);
        }
        JSONObject tree = mode.equals("screenshot") ? null : new JSONObject().put("available", snapshot.window != -1 && !localControl)
            .put("reason", localControl ? "LOCAL_CONTROL_UI" : JSONObject.NULL)
            .put("elements", nodes).put("collection_complete", complete[0]).put("collection_truncated", complete[1])
            .put("collected_nodes", count[0]).put("output_nodes", nodes.length()).put("output_bytes", nodes.toString().getBytes("UTF-8").length)
            .put("started_at_ms", System.currentTimeMillis() - (SystemClock.elapsedRealtime() - start))
            .put("finished_at_ms", System.currentTimeMillis() - (SystemClock.elapsedRealtime() - treeFinished));
        return new JSONObject().put("observation_id", id).put("service_instance_id", instanceId)
            .put("ui_version", version.get()).put("window_id", snapshot.window).put("package_name", snapshot.app)
            .put("captured_at_ms", System.currentTimeMillis()).put("tree", tree == null ? JSONObject.NULL : tree)
            .put("screenshot", shot == null ? JSONObject.NULL : shot).put("observation_status", "complete")
            .put("elapsed_ms", SystemClock.elapsedRealtime() - start).put("wait_ms", 0)
            .put("collection_ms", SystemClock.elapsedRealtime() - start)
            .put("display", new JSONObject().put("width", snapshot.width).put("height", snapshot.height).put("rotation", snapshot.rotation * 90));
    }

    private Snapshot context(JSONObject args) throws Exception {
        ready();
        if (args.has("service_instance_id") && !instanceId.equals(args.getString("service_instance_id"))) throw new Failure("STALE_OBSERVATION");
        Snapshot snapshot;
        synchronized (snapshots) { snapshot = snapshots.get(args.optString("observation_id")); }
        if (snapshot == null || SystemClock.elapsedRealtime() - snapshot.created > 30000 || !snapshot.generation.equals(instanceId)) throw new Failure("STALE_OBSERVATION");
        if (!args.has("element_id") && version.get() != snapshot.uiVersion) throw new Failure("STALE_OBSERVATION");
        int[] dimensions = geometry();
        if (dimensions[0] != snapshot.width || dimensions[1] != snapshot.height || dimensions[2] != snapshot.rotation) throw new Failure("STALE_OBSERVATION");
        AccessibilityNodeInfo root = getRootInActiveWindow();
        try {
            if (root == null || root.getWindowId() != snapshot.window || !snapshot.app.contentEquals(root.getPackageName())) throw new Failure("WINDOW_MISMATCH");
            if (getPackageName().contentEquals(root.getPackageName())) throw new Failure("LOCAL_CONTROL_UI");
        } finally { if (root != null) root.recycle(); }
        return snapshot;
    }

    private AccessibilityNodeInfo target(JSONObject args) throws Exception {
        Snapshot snapshot = context(args);
        String path = args.getString("element_id");
        String expected = snapshot.nodes.get(path);
        if (expected == null) throw new Failure("STALE_ELEMENT");
        AccessibilityNodeInfo node = getRootInActiveWindow();
        try {
            String[] pieces = path.split("\\.");
            for (int index = 1; index < pieces.length; index++) {
                AccessibilityNodeInfo child = node.getChild(Integer.parseInt(pieces[index])); node.recycle(); node = child;
                if (node == null) throw new Failure("STALE_ELEMENT");
            }
            if (!node.refresh() || !expected.equals(reference(node)) || !node.isEnabled() || !node.isVisibleToUser()) throw new Failure("STALE_ELEMENT");
            return node;
        } catch (Exception error) { if (node != null) node.recycle(); throw error; }
    }

    private void execute(String tool, JSONObject args, Request request) throws Exception {
        ready();
        if (request.cancelled) throw new Failure("CANCELLED");
        if (args.has("service_instance_id") && !instanceId.equals(args.getString("service_instance_id"))) throw new Failure("STALE_OBSERVATION");
        if (tool.equals("launch_app")) {
            Intent intent = getPackageManager().getLaunchIntentForPackage(args.getString("package_name"));
            if (intent == null) throw new Failure("APP_NOT_FOUND");
            startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            long end = SystemClock.elapsedRealtime() + 5000;
            while (SystemClock.elapsedRealtime() < end) {
                if (request.cancelled) throw new Failure("RESULT_UNKNOWN", true);
                AccessibilityNodeInfo root = getRootInActiveWindow();
                try { if (root != null && args.getString("package_name").contentEquals(root.getPackageName())) return; }
                finally { if (root != null) root.recycle(); }
                Thread.sleep(50);
            }
            throw new Failure("RESULT_UNKNOWN", true);
        }
        if (tool.equals("back") || tool.equals("home")) {
            if (!performGlobalAction(tool.equals("back") ? GLOBAL_ACTION_BACK : GLOBAL_ACTION_HOME)) throw new Failure("ACTION_REJECTED");
            return;
        }
        if (!args.has("element_id")) {
            if (localWindowVisible()) throw new Failure("LOCAL_CONTROL_UI");
            Snapshot snapshot = context(args);
            if (!tool.equals("tap") && !tool.equals("swipe")) throw new Failure("ACTION_UNSUPPORTED");
            JSONArray points = tool.equals("swipe") ? args.getJSONArray("points") : new JSONArray().put(new JSONObject().put("x", args.getDouble("x")).put("y", args.getDouble("y")));
            if (points.length() < 1 || points.length() > 64 || (tool.equals("swipe") && points.length() < 2)) throw new Failure("INVALID_ARGUMENT");
            Path path = new Path();
            for (int index = 0; index < points.length(); index++) {
                double x = points.getJSONObject(index).getDouble("x"), y = points.getJSONObject(index).getDouble("y");
                if (!Double.isFinite(x) || !Double.isFinite(y) || x < 0 || y < 0 || x >= snapshot.width || y >= snapshot.height) throw new Failure("INVALID_ARGUMENT");
                if (index == 0) path.moveTo((float)x, (float)y); else path.lineTo((float)x, (float)y);
            }
            CompletableFuture<Boolean> completed = new CompletableFuture<>();
            long duration = tool.equals("tap") ? 60 : Math.max(1, Math.min(1000, args.optInt("duration_ms", 300)));
            boolean accepted = dispatchGesture(new GestureDescription.Builder().addStroke(new GestureDescription.StrokeDescription(path, 0, duration)).build(), new GestureResultCallback() {
                @Override public void onCompleted(GestureDescription gesture) { completed.complete(true); }
                @Override public void onCancelled(GestureDescription gesture) { completed.complete(false); }
            }, null);
            if (!accepted) throw new Failure("ACTION_REJECTED");
            try { if (!completed.get(2, TimeUnit.SECONDS)) throw new Failure("RESULT_UNKNOWN", true); }
            catch (java.util.concurrent.TimeoutException error) { throw new Failure("RESULT_UNKNOWN", true); }
            return;
        }
        AccessibilityNodeInfo node = target(args);
        try {
            int action;
            switch (tool) {
                case "tap": action = AccessibilityNodeInfo.ACTION_CLICK; break;
                case "long_press": action = AccessibilityNodeInfo.ACTION_LONG_CLICK; break;
                case "scroll":
                    String direction = args.getString("direction");
                    if (!direction.equals("forward") && !direction.equals("backward")) throw new Failure("INVALID_ARGUMENT");
                    action = direction.equals("forward") ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD; break;
                case "input_text": action = AccessibilityNodeInfo.ACTION_SET_TEXT; break;
                default: throw new Failure("ACTION_UNSUPPORTED");
            }
            final int selectedAction = action;
            if (node.getActionList().stream().noneMatch(item -> item.getId() == selectedAction)) throw new Failure("ACTION_UNSUPPORTED");
            Bundle bundle = null;
            if (tool.equals("input_text")) {
                if (!node.isEditable() || node.isPassword() || !args.optString("mode", "replace").equals("replace")) throw new Failure("ACTION_UNSUPPORTED");
                String text = args.getString("text"); if (text.length() > 10000) throw new Failure("INVALID_ARGUMENT");
                bundle = new Bundle(); bundle.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            }
            context(args);
            if (request.cancelled) throw new Failure("CANCELLED");
            if (!node.performAction(action, bundle)) throw new Failure("ACTION_REJECTED");
        } finally { node.recycle(); }
    }

    static String canonical(Object value) throws Exception {
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject)value; List<String> keys = new ArrayList<>();
            object.keys().forEachRemaining(keys::add); Collections.sort(keys);
            List<String> fields = new ArrayList<>();
            for (String key : keys) fields.add(JSONObject.quote(key) + ":" + canonical(object.get(key)));
            return "{" + String.join(",", fields) + "}";
        }
        if (value instanceof JSONArray) {
            List<String> items = new ArrayList<>(); JSONArray array = (JSONArray)value;
            for (int index = 0; index < array.length(); index++) items.add(canonical(array.get(index)));
            return "[" + String.join(",", items) + "]";
        }
        return value instanceof String ? JSONObject.quote((String)value) : String.valueOf(value);
    }

    JSONObject call(String tool, JSONObject args) throws Exception {
        if (tool.equals("get_status") || tool.equals("cancel")) {
            Request request;
            synchronized (requests) { request = requests.get(args.optString("request_id")); }
            if (request == null) return failure("REQUEST_NOT_FOUND", false);
            if (tool.equals("cancel")) request.cancelled = true;
            return new JSONObject().put("request", request.result);
        }
        if (tool.equals("get_capabilities")) return new JSONObject().put("android_api", Build.VERSION.SDK_INT)
            .put("max_nodes", 1500).put("snapshot_ttl_ms", 30000).put("keep_alive", true)
            .put("text_modes", new JSONArray(List.of("replace"))).put("actions", new JSONArray(ACTIONS));
        if (tool.equals("wait_for_change")) {
            long start = SystemClock.elapsedRealtime(); long prior = args.getLong("ui_version");
            long timeout = Math.max(1, Math.min(1000, args.optInt("timeout_ms", 500)));
            synchronized (changes) {
                while (version.get() == prior && enabled) {
                    long remaining = timeout - (SystemClock.elapsedRealtime() - start);
                    if (remaining <= 0) break;
                    changes.wait(remaining);
                }
            }
            return new JSONObject().put("changed", version.get() != prior).put("ui_version", version.get());
        }
        Request request = null;
        if (ACTIONS.contains(tool)) {
            String id = args.getString("request_id");
            if (id.isEmpty() || id.length() > 128) return failure("INVALID_ARGUMENT", false);
            String signature = tool + canonical(args);
            synchronized (requests) {
                requests.values().removeIf(item -> SystemClock.elapsedRealtime() - item.created > 600000 && item.result != null && !item.result.optString("state").equals("executing"));
                request = requests.get(id);
                if (request != null) return request.signature.equals(signature) ? request.result : failure("REQUEST_ID_CONFLICT", false);
                if (requests.size() >= 256) return failure("BUSY", false);
                request = new Request(); request.signature = signature;
                request.result = new JSONObject().put("state", "executing").put("action_executed", JSONObject.NULL).put("request_id", id);
                requests.put(id, request);
            }
        }
        if (!work.tryLock(100, TimeUnit.MILLISECONDS)) {
            JSONObject result = failure("BUSY", false);
            if (request != null) request.result = result;
            return result;
        }
        try {
            if (tool.equals("observe")) return observe(args);
            if (tool.equals("list_apps")) {
                JSONArray apps = new JSONArray();
                Intent intent = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
                for (ResolveInfo item : getPackageManager().queryIntentActivities(intent, 0)) apps.put(new JSONObject()
                    .put("package_name", item.activityInfo.packageName).put("name", item.loadLabel(getPackageManager())));
                return new JSONObject().put("apps", apps);
            }
            if (request == null) return failure("UNKNOWN_TOOL", false);
            try {
                execute(tool, args, request);
                request.result = new JSONObject().put("state", "executed").put("action_executed", true).put("request_id", args.getString("request_id"));
            } catch (Failure error) { request.result = failure(error.code, error.uncertain).put("request_id", args.getString("request_id")); }
            catch (Exception error) { request.result = failure("RESULT_UNKNOWN", true).put("request_id", args.getString("request_id")); }
            JSONObject result = new JSONObject(request.result.toString());
            if (result.optBoolean("action_executed") && args.optBoolean("observe_after")) {
                try { result.put("observation", observe(args.optJSONObject("observation_options") == null ? new JSONObject() : args.getJSONObject("observation_options"))); }
                catch (Exception error) { result.put("observation_status", "failed").put("observation_error", error.getClass().getSimpleName()); }
            }
            return result;
        } catch (Failure error) { return failure(error.code, error.uncertain); }
        finally { work.unlock(); }
    }
}
