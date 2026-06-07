import Capacitor
import UIKit

// [kaveri fork] Register app-local Capacitor plugins. Capacitor 8's SPM model
// auto-registers only SPM-packaged plugins, not classes in the App target, so
// the native WebSocket plugin must be registered explicitly here.
class HermesBridgeViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(HermesWSPlugin())
        bridge?.registerPluginInstance(HermesGeofencePlugin())
    }

    // [kaveri fork] Safe-area insets from UIKit → CSS vars.
    //
    // WKWebView's CSS env(safe-area-inset-*) reports 0 until its first internal
    // layout pass, which only settles after something forces it (the keyboard
    // showing). With contentInset:'never' (edge-to-edge), that left the bottom
    // profile rail flush at the screen bottom on launch — its lower half fell in
    // the home-indicator system-gesture zone and ate taps until a keyboard
    // show/hide. Rather than trust env(), we read the controller view's REAL
    // safeAreaInsets (available immediately, correct from the first layout) and
    // publish them as --safe-top/right/bottom/left on <html>. The CSS consumes
    // these vars instead of env(). This callback fires on the first layout and on
    // every change (rotation, keyboard) so the vars always track the device.
    override open func viewSafeAreaInsetsDidChange() {
        super.viewSafeAreaInsetsDidChange()
        publishSafeAreaInsets()
    }

    override open func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        publishSafeAreaInsets()
    }

    private func publishSafeAreaInsets() {
        let insets = view.safeAreaInsets
        let js = """
        (function(){var s=document.documentElement.style;\
        s.setProperty('--safe-top','\(insets.top)px');\
        s.setProperty('--safe-right','\(insets.right)px');\
        s.setProperty('--safe-bottom','\(insets.bottom)px');\
        s.setProperty('--safe-left','\(insets.left)px');})();
        """
        webView?.evaluateJavaScript(js, completionHandler: nil)
    }
}
