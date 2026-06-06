import Capacitor
import UIKit

// [kaveri fork] Register app-local Capacitor plugins. Capacitor 8's SPM model
// auto-registers only SPM-packaged plugins, not classes in the App target, so
// the native WebSocket plugin must be registered explicitly here.
class HermesBridgeViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(HermesWSPlugin())
    }
}
