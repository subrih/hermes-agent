import Capacitor
import CoreLocation
import Foundation
import UIKit

// [kaveri fork] Background geofence monitoring for the Kaveri iOS client.
// Registers CLCircularRegions and, on enter/exit — which iOS delivers even when
// the app is backgrounded or killed (it relaunches us) — POSTs /api/event to the
// gateway, which runs an autonomous turn and pushes the result if it's worth it.
//
// The POST must work without JS loaded (a background region-event relaunch may
// not boot the WebView), so the connection details (url/token/CF headers) are
// stashed in UserDefaults by configure() and read natively at event time.
@objc(HermesGeofencePlugin)
public class HermesGeofencePlugin: CAPPlugin, CAPBridgedPlugin, CLLocationManagerDelegate {
    public let identifier = "HermesGeofencePlugin"
    public let jsName = "HermesGeofence"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "configure", returnType: CAPPluginReturnPromise)
    ]

    private let manager = CLLocationManager()
    private let defaults = UserDefaults.standard
    private let kURL = "hermes.geofence.url"
    private let kToken = "hermes.geofence.token"
    private let kCfId = "hermes.geofence.cfId"
    private let kCfSecret = "hermes.geofence.cfSecret"

    override public func load() {
        manager.delegate = self
        manager.allowsBackgroundLocationUpdates = true
        manager.pausesLocationUpdatesAutomatically = false
        manager.requestAlwaysAuthorization()
    }

    // configure({url, token, cfId, cfSecret, regions:[{id,lat,lon,radius}]})
    @objc func configure(_ call: CAPPluginCall) {
        if let v = call.getString("url") { defaults.set(v, forKey: kURL) }
        if let v = call.getString("token") { defaults.set(v, forKey: kToken) }
        if let v = call.getString("cfId") { defaults.set(v, forKey: kCfId) }
        if let v = call.getString("cfSecret") { defaults.set(v, forKey: kCfSecret) }
        manager.requestAlwaysAuthorization()

        // Re-register: drop regions no longer in the set, add the current ones.
        for r in manager.monitoredRegions {
            manager.stopMonitoring(for: r)
        }
        var count = 0
        for r in call.getArray("regions", JSObject.self) ?? [] {
            guard let id = r["id"] as? String,
                  let lat = r["lat"] as? Double,
                  let lon = r["lon"] as? Double else { continue }
            let radius = (r["radius"] as? Double) ?? 150
            let region = CLCircularRegion(
                center: CLLocationCoordinate2D(latitude: lat, longitude: lon),
                radius: radius,
                identifier: id
            )
            region.notifyOnEntry = true
            region.notifyOnExit = true
            manager.startMonitoring(for: region)
            count += 1
        }
        call.resolve(["ok": true, "count": count])
    }

    public func locationManager(_ manager: CLLocationManager, didEnterRegion region: CLRegion) {
        postEvent(region: region.identifier, action: "enter")
    }

    public func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        postEvent(region: region.identifier, action: "exit")
    }

    private func postEvent(region: String, action: String) {
        guard let base = defaults.string(forKey: kURL), !base.isEmpty,
              let token = defaults.string(forKey: kToken), !token.isEmpty,
              let url = URL(string: base.hasSuffix("/") ? base + "api/event" : base + "/api/event")
        else { return }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(token, forHTTPHeaderField: "X-Hermes-Session-Token")
        if let cfId = defaults.string(forKey: kCfId), !cfId.isEmpty {
            req.setValue(cfId, forHTTPHeaderField: "CF-Access-Client-Id")
        }
        if let cfSecret = defaults.string(forKey: kCfSecret), !cfSecret.isEmpty {
            req.setValue(cfSecret, forHTTPHeaderField: "CF-Access-Client-Secret")
        }
        req.httpBody = try? JSONSerialization.data(
            withJSONObject: ["type": "geofence", "region": region, "action": action]
        )

        // Keep the process alive long enough to finish the request on a
        // background region-event wake.
        var bg: UIBackgroundTaskIdentifier = .invalid
        bg = UIApplication.shared.beginBackgroundTask {
            if bg != .invalid { UIApplication.shared.endBackgroundTask(bg); bg = .invalid }
        }
        URLSession.shared.dataTask(with: req) { _, _, _ in
            if bg != .invalid { UIApplication.shared.endBackgroundTask(bg); bg = .invalid }
        }.resume()
    }
}
