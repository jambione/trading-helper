// notifier.swift — long-lived macOS notification helper for the trading agent.
//
// Why this exists:
//   terminal-notifier / osascript post through the legacy NSUserNotification API,
//   which macOS 26 (Tahoe) disabled — no banner, and osascript can't run a
//   command on click anyway. This app uses the modern UNUserNotificationCenter
//   API (real banners on Tahoe) AND handles clicks, so a click fires the
//   add-to-TradingView+Webull workflow on the Python agent.
//
// How it works:
//   • Runs as a background accessory app (no Dock icon) with proper bundle
//     identity (required for UNUserNotificationCenter) — see BrasfieldNotifier.app.
//   • Listens on 127.0.0.1:8890 for newline-delimited JSON requests:
//         {"title":"🔥 TSLA burst","subtitle":"$240.50",
//          "body":"7x mentions","ticker":"TSLA","sound":"Ping"}\n
//     and posts each as a native notification.
//   • On click, GETs http://localhost:<agentPort>/add?ticker=<T>&mode=both
//     so the running mac_agent runs the WB+TV workflow. No browser tab opens.
//
// Build:  scripts/build_notifier.sh  →  BrasfieldNotifier.app
// Run  :  open BrasfieldNotifier.app   (or launched from startup.command)

import AppKit
import Foundation
import Network
import UserNotifications

let LISTEN_PORT: UInt16 = 8890
// Where to send the add request when a notification is clicked. Overridable so
// the port can't drift from mac_agent's HTTP port.
let AGENT_PORT = ProcessInfo.processInfo.environment["AGENT_PORT"] ?? "8889"
let AGENT_ADD_URL = "http://localhost:\(AGENT_PORT)/add"

func logErr(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

// print(..., flush:) helper — stdout is line-buffered when piped; force flush.
func logOut(_ s: String) {
    Swift.print(s)
    fflush(stdout)
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var listener: NWListener?

    func applicationDidFinishLaunching(_ note: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, err in
            if let err = err { logErr("auth error: \(err)") }
            logOut("notifier: authorization granted=\(granted)")
            center.getNotificationSettings { s in
                logOut("notifier: authStatus=\(s.authorizationStatus.rawValue) "
                    + "alertSetting=\(s.alertSetting.rawValue) "
                    + "alertStyle=\(s.alertStyle.rawValue) "
                    + "soundSetting=\(s.soundSetting.rawValue) "
                    + "notificationCenter=\(s.notificationCenterSetting.rawValue)")
            }
        }
        startServer()
    }

    // ── Local control channel ────────────────────────────────────────────────
    func startServer() {
        do {
            let params = NWParameters.tcp
            params.allowLocalEndpointReuse = true
            params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1",
                                                     port: NWEndpoint.Port(rawValue: LISTEN_PORT)!)
            listener = try NWListener(using: params)
        } catch {
            logErr("listener init failed: \(error)")
            exit(1)
        }
        listener?.newConnectionHandler = { [weak self] conn in
            conn.start(queue: .main)
            self?.receive(on: conn, buffer: Data())
        }
        listener?.stateUpdateHandler = { state in
            if case .failed(let e) = state { logErr("listener failed: \(e)"); exit(1) }
        }
        listener?.start(queue: .main)
        logOut("notifier: listening on 127.0.0.1:\(LISTEN_PORT) → \(AGENT_ADD_URL)")
    }

    // Read until a newline, then treat everything before it as one JSON request.
    func receive(on conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }
            var buf = buffer
            if let data = data { buf.append(data) }
            if let nl = buf.firstIndex(of: 0x0A) {
                self.handleLine(Data(buf[buf.startIndex..<nl]))
                conn.cancel()
                return
            }
            if isComplete || error != nil {
                if !buf.isEmpty { self.handleLine(buf) }
                conn.cancel()
                return
            }
            self.receive(on: conn, buffer: buf)
        }
    }

    func handleLine(_ data: Data) {
        guard
            let obj = try? JSONSerialization.jsonObject(with: data),
            let json = obj as? [String: Any]
        else { logErr("bad JSON: \(String(data: data, encoding: .utf8) ?? "?")"); return }
        post(json)
    }

    // ── Post a native notification ───────────────────────────────────────────
    func post(_ json: [String: Any]) {
        let content = UNMutableNotificationContent()
        content.title = json["title"] as? String ?? "Alert"
        if let s = json["subtitle"] as? String, !s.isEmpty { content.subtitle = s }
        content.body = json["body"] as? String ?? ""
        content.sound = .default
        content.interruptionLevel = .active
        if let ticker = json["ticker"] as? String, !ticker.isEmpty {
            content.userInfo = ["ticker": ticker.uppercased()]
            content.threadIdentifier = "alert-\(ticker.uppercased())"  // coalesce per ticker
        }
        let req = UNNotificationRequest(identifier: UUID().uuidString,
                                        content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req) { err in
            if let err = err { logErr("add notification failed: \(err)") }
            else { logOut("notifier: posted '\(content.title)'") }
        }
    }

    // ── Notification delegate ────────────────────────────────────────────────
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        logOut("notifier: willPresent called → returning banner+sound+list")
        completionHandler([.banner, .sound, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let ticker = response.notification.request.content.userInfo["ticker"] as? String ?? ""
        if !ticker.isEmpty { fireAdd(ticker) }
        completionHandler()
    }

    func fireAdd(_ ticker: String) {
        let enc = ticker.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? ticker
        guard let url = URL(string: "\(AGENT_ADD_URL)?ticker=\(enc)&mode=both") else { return }
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: url) { _, _, err in
            if let err = err { logErr("add request failed for \(ticker): \(err)") }
            else { logOut("notifier: clicked → added \(ticker)") }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 4)
    }
}

let delegate = AppDelegate()
let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // background app, no Dock icon
app.delegate = delegate
app.run()
