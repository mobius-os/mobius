# Android APKs

Build a small installable Android APK on this instance: plain Java with Android's built-in Views, no Gradle or AndroidX, from a ~111 MB toolchain. `Read` this before installing Android tools, building, signing or delivering an APK, or when an APK won't download, install or launch.

---

## The route and its limits

One route: **plain native Java**. The app uses only the Android framework, so
the APK is tiny (often under 100 KB), builds in seconds with under 200 MB of
memory, and works offline. No Gradle, Kotlin compiler or AndroidX is involved;
the whole toolchain is about 111 MB.

The framework covers most personal apps: Views and Canvas drawing, sensors,
haptics, Camera2, audio, Bluetooth, `LocationManager`, local notifications,
`JobScheduler`, home-screen widgets, SQLite, SharedPreferences, WebView,
OpenGL ES, and networking (below).

Not possible with this toolchain; tell the owner instead of silently switching
to a heavier one:

- **Kotlin**, **AndroidX/Jetpack/Compose/Material** (their AAR packages need
  Gradle's resource and manifest merging). Use the framework equivalents.
- **Google Play services**: push via FCM, Maps SDK, Google sign-in, Play
  Billing, ads, fused location.
- **C/C++ code** (no NDK), **AIDL**, RenderScript.
- **APIs newer than Android platform 36.**
- **Play Store uploads** (an `.aab` needs bundletool). These APKs are for
  sideloading.
- **ARM64 hosts.** Google publishes the Linux build tools for x86-64 only, so
  `setup` refuses other machines before downloading anything. Tell the owner
  their Möbius host can't build APKs this way.
- **Automated UI tests** (no emulator here): verify on a real device.

A plain Java library shipped as a single `.jar` without resources does work:
put it in the project's `libs/` folder.

Before building, tell the owner the trade-offs once: UI is written in code
rather than with modern UI toolkits, and the APK is for their own devices, not
the Play Store.

## Toolchain

```bash
python3 -I "$SCRIPTS_DIR/android_toolchain.py" setup
```

It installs into `/opt/apk` (`--prefix` to change it). If that folder can't be
created, run `sudo -n mkdir -p /opt/apk && sudo -n chown "$(id -u):$(id -g)" /opt/apk`
first, or pass a prefix on the data disk if root is disabled. Before the first
run, tell the owner that installing Google's Android pieces means accepting
the Android SDK License Agreement (https://developer.android.com/studio/terms).

What it does, so you can explain it:

- **Android:** reads only `android.jar` (platform 36) and five build tools out
  of Google's archives with HTTP range requests, about 50 MB transferred.
- **Java:** streams the Temurin JDK 21 archive from Adoptium (about 210 MB,
  checksum-verified), keeps only what `jlink` needs in a temporary folder,
  builds a ~58 MB runtime with `java`, `javac` and `keytool`, then deletes the
  temporary folder.
- Peak disk about 300 MB, final about 111 MB. Each part is assembled in a
  staging folder and moved into place only when complete, so after an
  interrupted run just run `setup` again; complete parts are skipped.
- Creates a per-instance **test signing key** in
  `/data/native-apps/.signing/test/` (`test.jks` plus `signing.env` with a
  random password, both owner-only) when none exists. An existing key is never
  replaced; if that folder is incomplete, `setup` stops and says so.

`/opt` is lost when the container is replaced; `setup` restores it in about a
minute. The signing key and project sources live on the data disk so they
survive. Never print the key's password in chat.

A "module not found" error from `javac`, `d8` or `apksigner` means the runtime
needs one more module: add it to `JLINK_MODULES` (and its dependencies to
`JMOD_CLOSURE`) in the script, delete `/opt/apk/jre` and rerun `setup`.

## A project

Keep each app as its own git repository under `/data/native-apps/<name>/`:

```
AndroidManifest.xml
res/values/strings.xml          (and res/mipmap-*/ic_launcher.png for an icon)
src/<package path>/MainActivity.java
libs/*.jar                      (optional single-jar libraries)
```

Give every app its own package id, for example `you.mobius.<owner>.<name>`.
A minimal manifest and activity:

```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="you.mobius.example.hello">
    <application android:label="@string/app_name"
        android:theme="@android:style/Theme.Material.NoActionBar">
        <activity android:name=".MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
```

```java
package you.mobius.example.hello;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

public class MainActivity extends Activity {
  @Override protected void onCreate(Bundle state) {
    super.onCreate(state);
    TextView text = new TextView(this);
    text.setText("Hello");
    setContentView(text);
  }
}
```

## Build

```bash
python3 -I "$SCRIPTS_DIR/android_toolchain.py" build /data/native-apps/<name> \
  --version-code 2 --version-name 0.1.1
```

It runs `aapt2 compile` and `link` (min SDK 24, target 36, an `R` class
generated for code that needs it), `javac` at Java 8 level, `d8`, `zipalign`
and `apksigner`, and writes `build/<name>.apk`. Pass `--signing-env` for a
different key. **Raise `--version-code` for every APK you hand out**, or
Android won't install it over the previous one. Lambdas and method references
work; d8 desugars them.

Code shape that works:

- `Activity` (not AppCompat) with `@android:style/Theme.Material.NoActionBar`.
  Build the UI in code; use `GradientDrawable` for rounded shapes and a custom
  `View.onDraw` for charts and dials, animating with
  `postInvalidateOnAnimation()`.
- Configure system bars **after** `setContentView`; before it,
  `getWindow().getInsetsController()` crashes at launch.
- Target SDK 35+ draws edge to edge: pad the root from
  `setOnApplyWindowInsetsListener` (system bars and display cutout) and set
  light/dark bar icons yourself.
- Pick colours from `Configuration.UI_MODE_NIGHT_MASK` and leave `uiMode` out
  of `configChanges` so a dark-mode switch recreates the activity.
- Nothing lints API levels here: guard calls above API 24 with
  `Build.VERSION.SDK_INT` (for example `TextView.setLineHeight` is API 28).
- For fast sensor updates, only change text when it differs and rebuild
  drawables only on state changes. Start sensors in `onResume` and stop them
  in `onPause`.

## Networking without libraries

`HttpURLConnection` covers GET/POST, headers, redirects, gzip, timeouts and
HTTPS with the system certificates; `org.json` and `javax.xml.parsers` handle
JSON and RSS/XML. (`java.net.http.HttpClient` does not exist on Android.)

- Add `<uses-permission android:name="android.permission.INTERNET" />`.
- Never fetch on the main thread: use a background `Executor` and post results
  back with `runOnUiThread`.
- Plain `http://` is blocked by default; use HTTPS or a network security
  config for specific hosts.
- A native app has no browser CORS, so it can call sites a mini-app would need
  a proxy for.
- Keep parsing classes free of `android.*` imports so they compile and run on
  this instance's `java` against live data: the only pre-device test available.
  `org.json` is Android-only, so keep it out of those classes.

## Verify and deliver

```bash
T=/opt/apk; BT=$T/sdk/build-tools/36.0.0; export PATH=$T/jre/bin:$PATH
$BT/apksigner verify --print-certs build/<name>.apk
$BT/aapt2 dump badging build/<name>.apk | head -1   # package, versionCode
```

Copy the APK with a descriptive name into the chat's generated-files folder
(`$MOBIUS_GENERATED_DIR`) so it appears as a download. Tell the owner the
package id, version and size, whether it updates an earlier install, and what
to check on the device; say plainly that sensors, haptics and launch were not
device-verified here.

## When it won't download, install or launch

Find out where the owner tapped before diagnosing:

- **Tapping the download inside the Möbius phone app does nothing:** open this
  Möbius in the phone's browser and download there.
- **Downloaded, but tapping it does nothing:** open it from the Files app,
  allow "Install unknown apps" for that app, and on Play Protect choose
  "Install anyway". Some phones (for example Samsung's Auto Blocker) block
  sideloading until the setting is turned off.
- **"App not installed":** a different signing key or a lower version code
  than the installed copy. Uninstall it, or rebuild with the same key and a
  higher version code.
- **Installs but crashes on launch:** ask for the crash log
  (`adb logcat -b crash` from a computer) and check the code-shape notes above.
