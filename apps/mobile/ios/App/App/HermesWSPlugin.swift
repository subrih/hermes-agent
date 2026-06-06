import Capacitor
import Foundation

// [kaveri fork] Native WebSocket for the Kaveri/Hermes iOS client.
// A WKWebView browser WebSocket can't set Cloudflare Access headers on the
// upgrade, so the gateway WS is opened natively via URLSessionWebSocketTask
// (which CAN set headers). The JS side (platform/ios-bridge.ts) wraps this as a
// WebSocketLike and feeds it to the gateway client's socketFactory.
@objc(HermesWSPlugin)
public class HermesWSPlugin: CAPPlugin, CAPBridgedPlugin, URLSessionWebSocketDelegate {
    public let identifier = "HermesWSPlugin"
    public let jsName = "HermesWS"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "connect", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "send", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "close", returnType: CAPPluginReturnPromise)
    ]

    private var tasks: [Int: URLSessionWebSocketTask] = [:]
    private var idByTask: [Int: Int] = [:]
    private var nextId = 0
    private lazy var session: URLSession = {
        URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    }()

    @objc func connect(_ call: CAPPluginCall) {
        guard let urlStr = call.getString("url"), let url = URL(string: urlStr) else {
            call.reject("invalid url")
            return
        }
        let headers = call.getObject("headers") ?? [:]
        var req = URLRequest(url: url)
        for (k, v) in headers {
            if let s = v as? String { req.setValue(s, forHTTPHeaderField: k) }
        }
        nextId += 1
        let id = nextId
        let task = session.webSocketTask(with: req)
        tasks[id] = task
        idByTask[task.taskIdentifier] = id
        task.resume()
        receive(id: id, task: task)
        call.resolve(["id": id])
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                           didOpenWithProtocol proto: String?) {
        if let id = idByTask[webSocketTask.taskIdentifier] {
            notifyListeners("wsEvent", data: ["id": id, "type": "open"])
        }
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                           didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        if let id = idByTask[webSocketTask.taskIdentifier] {
            notifyListeners("wsEvent", data: ["id": id, "type": "close"])
            tasks[id] = nil
        }
        idByTask[webSocketTask.taskIdentifier] = nil
    }

    private func receive(id: Int, task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                self.notifyListeners("wsEvent", data: ["id": id, "type": "error", "data": err.localizedDescription])
                self.notifyListeners("wsEvent", data: ["id": id, "type": "close"])
                self.tasks[id] = nil
            case .success(let message):
                switch message {
                case .string(let text):
                    self.notifyListeners("wsEvent", data: ["id": id, "type": "message", "data": text])
                case .data(let d):
                    self.notifyListeners("wsEvent", data: ["id": id, "type": "message", "data": String(data: d, encoding: .utf8) ?? ""])
                @unknown default:
                    break
                }
                self.receive(id: id, task: task)
            }
        }
    }

    @objc func send(_ call: CAPPluginCall) {
        guard let id = call.getInt("id"), let data = call.getString("data"), let task = tasks[id] else {
            call.reject("no socket")
            return
        }
        task.send(.string(data)) { err in
            if let err = err { call.reject(err.localizedDescription) } else { call.resolve() }
        }
    }

    @objc func close(_ call: CAPPluginCall) {
        guard let id = call.getInt("id"), let task = tasks[id] else {
            call.resolve()
            return
        }
        task.cancel(with: .normalClosure, reason: nil)
        tasks[id] = nil
        call.resolve()
    }
}
