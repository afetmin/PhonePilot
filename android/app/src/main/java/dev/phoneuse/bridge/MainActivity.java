package dev.phoneuse.bridge;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import java.net.Inet4Address;
import java.net.NetworkInterface;
import java.util.Collections;

public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView status, address, pairing;
    private Button approve, deny;
    private final Runnable refresh = new Runnable() {
        @Override public void run() { update(); handler.postDelayed(this, 500); }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        ScrollView scroll = new ScrollView(this);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return insets;
        });
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int padding = (int)(20 * getResources().getDisplayMetrics().density);
        layout.setPadding(padding, padding * 2, padding, padding);
        scroll.addView(layout); setContentView(scroll);
        TextView title = text(layout, "Phone Bridge", 24);
        title.setAccessibilityHeading(true);
        status = text(layout, "", 16);
        Button settings = button(layout, "Open accessibility settings");
        settings.setOnClickListener(view -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        Switch control = new Switch(this);
        control.setText("Allow phone control");
        control.setChecked(getSharedPreferences("bridge", 0).getBoolean("enabled", true));
        layout.addView(control);
        control.setOnCheckedChangeListener((button, checked) -> {
            getSharedPreferences("bridge", 0).edit().putBoolean("enabled", checked).apply();
            if (BridgeService.current != null) BridgeService.current.setControl(checked);
        });
        address = text(layout, "", 16);
        address.setTextIsSelectable(true);
        button(layout, "Copy address").setOnClickListener(view -> {
            String selected = address.getText().toString().split("\n")[0];
            getSystemService(ClipboardManager.class).setPrimaryClip(ClipData.newPlainText("Phone Bridge address", selected));
        });
        pairing = text(layout, "", 24);
        pairing.setMinHeight((int)(64 * getResources().getDisplayMetrics().density));
        approve = button(layout, "Codes match: authorize");
        deny = button(layout, "Reject pairing");
        approve.setOnClickListener(view -> { if (server() != null) server().approve(); update(); });
        deny.setOnClickListener(view -> { if (server() != null) server().deny(); update(); });
        button(layout, "Revoke computer access").setOnClickListener(view -> {
            if (server() != null) server().revoke(); update();
        });
    }

    private TextView text(LinearLayout layout, String value, int size) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(size);
        view.setPadding(0, 16, 0, 16); layout.addView(view); return view;
    }

    private Button button(LinearLayout layout, String label) {
        Button button = new Button(this); button.setText(label); layout.addView(button); return button;
    }

    private BridgeServer server() { return BridgeService.current == null ? null : BridgeService.current.server; }

    private void update() {
        BridgeService service = BridgeService.current;
        status.setText(service == null ? "Accessibility permission required" : !service.enabled ? "Control disabled" :
            service.server == null ? "Starting" + (service.serverError == null ? "" : ": " + service.serverError) : "Connected to local network");
        StringBuilder addresses = new StringBuilder();
        try {
            for (NetworkInterface network : Collections.list(NetworkInterface.getNetworkInterfaces()))
                for (java.net.InetAddress ip : Collections.list(network.getInetAddresses()))
                    if (ip instanceof Inet4Address && !ip.isLoopbackAddress()) {
                        if (addresses.length() > 0) addresses.append("\n");
                        addresses.append("https://").append(ip.getHostAddress()).append(":8443");
                    }
        } catch (Exception ignored) { }
        address.setText(addresses.length() == 0 ? "No network address" : addresses.toString());
        String code = server() == null ? null : server().pairingCode();
        pairing.setText(code == null ? "" : code);
        approve.setEnabled(code != null); deny.setEnabled(code != null);
    }

    @Override protected void onResume() { super.onResume(); handler.post(refresh); }
    @Override protected void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
}
