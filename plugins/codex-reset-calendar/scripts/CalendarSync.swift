import EventKit
import Foundation

private let calendarTitle = "Codex 重置卡"
private let eventTitle = "Codex 重置卡到期"
private let legacyTitlePrefix = eventTitle + " · "
private let accountNotePrefix = "Codex 账号："
private let legacyMarkerPrefix = "codex-reset-calendar://card/"
private let usageMarkerPrefix = "https://chatgpt.com/settings/usage?tab=overview#codex-reset-calendar/"
private let alarmPolicyVersion = "v2"
private let day: TimeInterval = 24 * 60 * 60
private let scanWindow: TimeInterval = 3 * 365 * day
private let calendarKey = "ManagedCalendarIdentifier"
private let scanKey = "ManagedEventLatestExpiry"

private struct SyncError: Error {
    let message: String

    init(_ message: String) {
        self.message = message
    }
}

private struct Card: Decodable {
    let id: String
    let expiresAt: TimeInterval
}

private struct Counts: Encodable {
    var created = 0
    var updated = 0
    var removed = 0
}

private struct Result: Encodable {
    let ok: Bool
    var counts: Counts? = nil
    var verified: Int? = nil
    var expected: Int? = nil
    var missing: Int? = nil
    var duplicates: Int? = nil
    var alarms: Int? = nil
    var linked: Int? = nil
    var legacyLinks: Int? = nil
    var labeled: Int? = nil
    var error: String? = nil
}

private struct ManagedEvent {
    let id: String
    let scope: String
    let hasCurrentAlarmPolicy: Bool
    let hasUsageLink: Bool
    let event: EKEvent
}

private struct Options {
    let command: String
    var inputFile: String?
    var permissionFile: String?
    var scope: String?
    var accountEmail: String?
    var prune = false

    init(_ arguments: [String]) throws {
        guard let command = arguments.first, ["authorize", "sync", "verify"].contains(command) else {
            throw SyncError("用法：CalendarSync authorize、sync 或 verify。")
        }
        self.command = command
        var accountEmailFile: String?
        var rest = arguments.dropFirst().makeIterator()
        while let argument = rest.next() {
            switch argument {
            case "--prune":
                prune = true
            case "--input-file", "--result-file", "--permission-file", "--scope", "--account-email-file":
                guard let value = rest.next(), !value.isEmpty, !value.hasPrefix("--") else {
                    throw SyncError("日历同步选项缺少值。")
                }
                switch argument {
                case "--input-file": inputFile = value
                case "--result-file": break
                case "--permission-file": permissionFile = value
                case "--account-email-file": accountEmailFile = value
                default:
                    guard isHash(value) else {
                        throw SyncError("日历同步收到无效的账号标识。")
                    }
                    scope = value
                }
            default:
                throw SyncError("无法识别日历同步选项。")
            }
        }
        if let accountEmailFile {
            guard scope != nil, command != "authorize" else {
                throw SyncError("只有在同步或核对已知账号时才能标注账号邮箱。")
            }
            guard let data = try? Data(contentsOf: URL(fileURLWithPath: accountEmailFile)),
                  let email = String(data: data, encoding: .utf8), validAccountEmail(email) else {
                throw SyncError("无法读取有效的账号邮箱。")
            }
            accountEmail = email
        }
    }
}

private func validAccountEmail(_ email: String) -> Bool {
    let excluded = CharacterSet.whitespacesAndNewlines.union(.controlCharacters)
    guard email.unicodeScalars.count <= 254, email.rangeOfCharacter(from: excluded) == nil else { return false }
    let parts = email.split(separator: "@", omittingEmptySubsequences: false)
    guard parts.count == 2, !parts[0].isEmpty else { return false }
    let domain = parts[1].split(separator: ".", omittingEmptySubsequences: false)
    return domain.count >= 2 && domain.allSatisfy { part in
        !part.isEmpty && part.first != "-" && part.last != "-" &&
            part.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" }
    }
}

private struct AccountNoteLine {
    let email: String
    let contentRange: Range<String.Index>
    let lineRange: Range<String.Index>
}

private func accountNoteLines(in notes: String?) -> [AccountNoteLine] {
    guard let notes, notes.contains(accountNotePrefix) else { return [] }
    var result: [AccountNoteLine] = []
    notes.enumerateSubstrings(in: notes.startIndex..<notes.endIndex, options: .byLines) { line, content, full, _ in
        guard let line, line.hasPrefix(accountNotePrefix) else { return }
        result.append(AccountNoteLine(
            email: String(line.dropFirst(accountNotePrefix.count)), contentRange: content, lineRange: full
        ))
    }
    return result
}

private func updatingAccountNote(in notes: String?, email: String) -> String {
    let newLine = accountNotePrefix + email
    guard let notes, !notes.isEmpty else { return newLine }
    let lines = accountNoteLines(in: notes)
    guard let first = lines.first else {
        if notes.last?.isNewline == true { return notes + newLine }
        let newline = notes.last(where: \.isNewline).map(String.init) ?? "\n"
        return notes + newline + newLine
    }

    var updated = String(notes[..<first.contentRange.lowerBound]) + newLine
    var start = first.contentRange.upperBound
    for line in lines.dropFirst() {
        updated += notes[start..<line.lineRange.lowerBound]
        start = line.lineRange.upperBound
    }
    updated += notes[start...]
    return updated
}

private func isHash(_ value: String) -> Bool {
    let bytes = value.utf8
    return bytes.count == 64 && bytes.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}

private func eventMarker(
    in url: String
) -> (id: String, scope: String, hasCurrentAlarmPolicy: Bool, hasUsageLink: Bool)? {
    let hasUsageLink = url.hasPrefix(usageMarkerPrefix)
    let prefix = hasUsageLink ? usageMarkerPrefix : legacyMarkerPrefix
    guard url.hasPrefix(prefix) else { return nil }
    let fields = url.dropFirst(prefix.count).split(separator: "/", omittingEmptySubsequences: false)
    guard fields.count == 3 else { return nil }
    let hasCurrentAlarmPolicy = fields[0] == alarmPolicyVersion
    guard hasCurrentAlarmPolicy || (!hasUsageLink && fields[0] == "v1") else { return nil }
    let scope = String(fields[1])
    let id = String(fields[2])
    guard (scope == "-" || isHash(scope)), isHash(id) else { return nil }
    return (id, scope, hasCurrentAlarmPolicy, hasUsageLink)
}

private func managedEvent(_ event: EKEvent) -> ManagedEvent? {
    guard !event.hasAttendees, !event.hasRecurrenceRules,
          let url = event.url?.absoluteString, let marker = eventMarker(in: url) else {
        return nil
    }
    return ManagedEvent(
        id: marker.id, scope: marker.scope, hasCurrentAlarmPolicy: marker.hasCurrentAlarmPolicy,
        hasUsageLink: marker.hasUsageLink, event: event
    )
}

private func removingLegacyLink(from notes: String?, id: String, scope: String) -> String? {
    guard let notes, notes.contains(legacyMarkerPrefix) else { return notes }
    var removedLines: [Range<String.Index>] = []
    notes.enumerateSubstrings(in: notes.startIndex..<notes.endIndex, options: .byLines) { line, _, range, _ in
        guard let line, let marker = eventMarker(in: line.trimmingCharacters(in: .whitespaces)),
              !marker.hasUsageLink, marker.id == id, marker.scope == scope || marker.scope == "-" else { return }
        removedLines.append(range)
    }
    guard !removedLines.isEmpty else { return notes }

    var kept = ""
    var start = notes.startIndex
    for range in removedLines {
        kept += notes[start..<range.lowerBound]
        start = range.upperBound
    }
    kept += notes[start...]
    return kept.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : kept
}

private func futureCards(from inputFile: String?, after now: Date) throws -> [String: Date] {
    let cards: [Card]
    do {
        let data: Data
        if let inputFile {
            data = try Data(contentsOf: URL(fileURLWithPath: inputFile))
        } else {
            data = FileHandle.standardInput.readDataToEndOfFile()
        }
        cards = try JSONDecoder().decode([Card].self, from: data)
    } catch {
        throw SyncError("无法读取重置卡同步数据。")
    }

    var result: [String: Date] = [:]
    for card in cards {
        guard isHash(card.id), card.expiresAt.isFinite,
              (0...253_402_300_799).contains(card.expiresAt) else {
            throw SyncError("日历同步收到无效的重置卡标识或到期时间。")
        }
        let date = Date(timeIntervalSince1970: card.expiresAt)
        guard date > now else { continue }
        if let current = result[card.id], current != date {
            throw SyncError("同一张重置卡有两个到期时间，已跳过本次同步。")
        }
        result[card.id] = date
    }
    return result
}

private func cloudSource(in store: EKEventStore) throws -> EKSource {
    // EventKit groups other CalDAV accounts with iCloud. Require Apple's account name as well.
    let sources = store.sources.filter {
        $0.sourceType == .calDAV && $0.title == "iCloud" && !$0.isDelegate
    }
    guard sources.count == 1, let source = sources.first else {
        throw SyncError("无法确认唯一的 iCloud 日历账号。请检查 Mac 上的 iCloud 日历设置。")
    }
    return source
}

private func writable(_ calendar: EKCalendar) -> Bool {
    calendar.type == .calDAV && calendar.allowsContentModifications && !calendar.isSubscribed
}

private func futureEvents(
    in calendar: EKCalendar, store: EKEventStore, after now: Date, through end: Date
) -> [EKEvent] {
    var events: [EKEvent] = []
    var seen: Set<String> = []
    var start = now
    while start < end {
        // EventKit silently shortens predicates covering more than four years.
        let stop = min(start.addingTimeInterval(scanWindow), end)
        let predicate = store.predicateForEvents(withStart: start, end: stop, calendars: [calendar])
        for event in store.events(matching: predicate) where event.startDate > now {
            if let eventIdentifier = event.eventIdentifier, seen.insert(eventIdentifier).inserted {
                events.append(event)
            }
        }
        start = stop
    }
    return events
}

private func findManagedCalendar(
    in source: EKSource, store: EKEventStore, now: Date, end: Date
) throws -> EKCalendar? {
    let calendars = source.calendars(for: .event)
    if let identifier = UserDefaults.standard.string(forKey: calendarKey),
       let calendar = calendars.first(where: { $0.calendarIdentifier == identifier }) {
        return calendar
    }

    // A full iCloud resync can replace the local ID. Recover by our event marker, never by title alone.
    let recoveryRange = now.addingTimeInterval(-365 * day)
    let recovered = calendars.filter { calendar in
        calendar.title == calendarTitle && writable(calendar) &&
            futureEvents(in: calendar, store: store, after: recoveryRange, through: end)
                .contains { managedEvent($0) != nil }
    }
    if !recovered.isEmpty {
        guard recovered.count == 1, let calendar = recovered.first else {
            throw SyncError("发现多个插件创建的重置卡日历，无法安全选择写入位置。")
        }
        return calendar
    }
    return nil
}

private func createManagedCalendar(in source: EKSource, store: EKEventStore) throws -> EKCalendar {
    let calendar = EKCalendar(for: .event, eventStore: store)
    calendar.title = calendarTitle
    calendar.source = source
    do {
        try store.saveCalendar(calendar, commit: true)
    } catch {
        throw SyncError("无法在 iCloud 中创建重置卡日历。")
    }
    return calendar
}

private func alarmOffsets(to expiry: Date, now: Date) -> [TimeInterval] {
    let remaining = expiry.timeIntervalSince(now)
    let future = [7 * day, 3 * day, day, 3600].filter { $0 < remaining }.map { -$0 }
    if !future.isEmpty { return future }
    return remaining > 300 ? [-300] : [0]
}

private func requireCalendarAccess() throws {
    guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else {
        throw SyncError("请先打开重置卡同步工具，允许它完整访问日历。")
    }
}

private func eventIndex(
    in calendar: EKCalendar, store: EKEventStore, after now: Date, through end: Date
) -> [String: [ManagedEvent]] {
    let events = futureEvents(in: calendar, store: store, after: now, through: end).compactMap(managedEvent)
    return Dictionary(grouping: events, by: \.id)
}

private func synchronize(_ options: Options, store: EKEventStore) throws -> Counts {
    try requireCalendarAccess()

    let now = Date()
    let cards = try futureCards(from: options.inputFile, after: now)
    let canPrune = options.prune && options.scope != nil
    guard !cards.isEmpty || canPrune else { return Counts() }

    let scope = options.scope ?? "-"
    let preferences = UserDefaults.standard
    let previousEnd = Date(timeIntervalSince1970: preferences.double(forKey: scanKey))
    let latestExpiry = cards.values.max() ?? now
    let end = max(now.addingTimeInterval(scanWindow), max(previousEnd, latestExpiry).addingTimeInterval(day))
    let source = try cloudSource(in: store)
    let calendar: EKCalendar
    if let current = try findManagedCalendar(in: source, store: store, now: now, end: end) {
        guard writable(current) else {
            throw SyncError("之前创建的重置卡日历目前无法写入。")
        }
        calendar = current
    } else {
        guard !cards.isEmpty else { return Counts() }
        calendar = try createManagedCalendar(in: source, store: store)
    }
    if preferences.string(forKey: calendarKey) != calendar.calendarIdentifier {
        preferences.set(calendar.calendarIdentifier, forKey: calendarKey)
    }

    var counts = Counts()
    var existing = eventIndex(in: calendar, store: store, after: now, through: end)

    // Record the upper limit before commit so a later authoritative run can find long-lived events.
    if latestExpiry > previousEnd {
        preferences.set(latestExpiry.timeIntervalSince1970, forKey: scanKey)
    }
    do {
        for (id, expiry) in cards {
            let candidates = existing.removeValue(forKey: id) ?? []
            // A read without an account ID can see the card, but cannot establish ownership.
            if scope == "-" && candidates.contains(where: { $0.scope != "-" }) { continue }
            let scoped = candidates.filter { $0.scope == scope }
            let unscoped = scope == "-" ? [] : candidates.filter { $0.scope == "-" }
            let matches = scoped + unscoped
            let match = matches.first
            let event = match?.event ?? EKEvent(eventStore: store)
            let isNew = matches.isEmpty
            let timeChanged = isNew || event.startDate != expiry
            let linkChanged = match?.hasUsageLink != true || match?.scope != scope
            let refreshAlarms = timeChanged || match?.hasCurrentAlarmPolicy != true
            let end = expiry.addingTimeInterval(60)
            let scheduleChanged = timeChanged || event.endDate != end || event.isAllDay
            let currentTitle = event.title
            let notes = event.notes
            let cleanedNotes = removingLegacyLink(from: notes, id: id, scope: match?.scope ?? scope)
            let updatedNotes = options.accountEmail.map { updatingAccountNote(in: cleanedNotes, email: $0) } ?? cleanedNotes
            let hasStoredEmail = options.scope != nil && accountNoteLines(in: updatedNotes).contains {
                validAccountEmail($0.email)
            }
            let keepLegacyTitle = !hasStoredEmail && currentTitle?.hasPrefix(legacyTitlePrefix) == true
            let title = keepLegacyTitle ? currentTitle ?? eventTitle : eventTitle
            let titleChanged = currentTitle != title
            let notesChanged = notes != updatedNotes
            if refreshAlarms || linkChanged || scheduleChanged || titleChanged || notesChanged {
                if scheduleChanged {
                    event.calendar = calendar
                    event.isAllDay = false
                    event.startDate = expiry
                    event.endDate = end
                    if calendar.supportedEventAvailabilities.contains(.free) {
                        event.availability = .free
                    }
                }
                if titleChanged {
                    event.title = title
                }
                if linkChanged {
                    event.url = URL(string: "\(usageMarkerPrefix)\(alarmPolicyVersion)/\(scope)/\(id)")
                }
                if notesChanged {
                    event.notes = updatedNotes
                }
                if refreshAlarms {
                    event.alarms = alarmOffsets(to: expiry, now: now).map { EKAlarm(relativeOffset: $0) }
                }
                try store.save(event, span: .thisEvent, commit: false)
                if isNew { counts.created += 1 } else { counts.updated += 1 }
            }
            for duplicate in matches.dropFirst() {
                try store.remove(duplicate.event, span: .thisEvent, commit: false)
                counts.removed += 1
            }
        }
        if canPrune {
            for candidate in existing.values.joined() where candidate.scope == scope {
                try store.remove(candidate.event, span: .thisEvent, commit: false)
                counts.removed += 1
            }
        }
        if counts.created + counts.updated + counts.removed > 0 {
            try store.commit()
        }
    } catch {
        store.reset()
        throw SyncError("无法保存 iCloud 日历的变更。下次同步会重试。")
    }
    return counts
}

private func verify(_ options: Options, store: EKEventStore) throws -> Result {
    try requireCalendarAccess()
    let now = Date()
    let cards = try futureCards(from: options.inputFile, after: now)
    guard let latestExpiry = cards.values.max() else {
        return Result(
            ok: true, verified: 0, expected: 0, missing: 0,
            duplicates: 0, alarms: 0, linked: 0, legacyLinks: 0, labeled: 0
        )
    }

    let previousEnd = Date(timeIntervalSince1970: UserDefaults.standard.double(forKey: scanKey))
    let end = max(now.addingTimeInterval(scanWindow), max(previousEnd, latestExpiry).addingTimeInterval(day))
    let source = try cloudSource(in: store)
    guard let calendar = try findManagedCalendar(in: source, store: store, now: now, end: end) else {
        return Result(
            ok: true, verified: 0, expected: cards.count, missing: cards.count,
            duplicates: 0, alarms: 0, linked: 0, legacyLinks: 0, labeled: 0
        )
    }
    let existing = eventIndex(in: calendar, store: store, after: now, through: end)
    var verified = 0
    var missing = 0
    var duplicates = 0
    var alarms = 0
    var linked = 0
    var legacyLinks = 0
    var labeled = 0
    for (id, expiry) in cards {
        let candidates = (existing[id] ?? []).filter {
            options.scope == nil || $0.scope == options.scope || $0.scope == "-"
        }
        let matches = candidates.filter {
            (options.scope == nil || $0.scope == options.scope) &&
                $0.event.startDate == expiry && !$0.event.isAllDay
        }
        duplicates += max(0, candidates.count - 1)
        if matches.isEmpty {
            missing += 1
        } else if candidates.count == 1, let match = matches.first {
            verified += 1
            alarms += match.event.alarms?.count ?? 0
            if match.hasUsageLink { linked += 1 }
            if match.event.notes?.contains(legacyMarkerPrefix) == true { legacyLinks += 1 }
            let accountLines = accountNoteLines(in: match.event.notes)
            if match.event.title == eventTitle, accountLines.count == 1, let email = accountLines.first?.email,
               options.accountEmail.map({ $0 == email }) ?? validAccountEmail(email) {
                labeled += 1
            }
        }
    }
    return Result(
        ok: true, verified: verified, expected: cards.count, missing: missing,
        duplicates: duplicates, alarms: alarms, linked: linked, legacyLinks: legacyLinks, labeled: labeled
    )
}

@main
private struct CalendarSync {
    @MainActor
    static func main() async {
        let arguments = Array(CommandLine.arguments.dropFirst().filter { !$0.hasPrefix("-psn_") })
        let resultFile: String? = arguments.firstIndex(of: "--result-file").flatMap { index in
            guard arguments.indices.contains(index + 1) else { return nil }
            let path = arguments[index + 1]
            return path.isEmpty || path.hasPrefix("--") ? nil : path
        }
        let result: Result
        do {
            let options = try Options(arguments)
            let store = EKEventStore()
            if options.command == "authorize" {
                let status = EKEventStore.authorizationStatus(for: .event)
                if status != .fullAccess {
                    guard status == .notDetermined || status == .writeOnly else {
                        throw SyncError("未获得完整日历权限。请在系统设置的‘隐私与安全性 > 日历’中检查权限。")
                    }
                    if let path = options.permissionFile {
                        do {
                            try Data().write(to: URL(fileURLWithPath: path), options: .atomic)
                        } catch {
                            throw SyncError("无法记录日历授权请求状态。")
                        }
                    }
                    guard (try? await store.requestFullAccessToEvents()) == true else {
                        throw SyncError("未获得完整日历权限。请在系统设置的‘隐私与安全性 > 日历’中检查权限。")
                    }
                }
                result = Result(ok: true)
            } else if options.command == "verify" {
                result = try verify(options, store: store)
            } else {
                result = Result(ok: true, counts: try synchronize(options, store: store))
            }
        } catch let error as SyncError {
            result = Result(ok: false, error: error.message)
        } catch {
            result = Result(ok: false, error: "日历同步失败。")
        }

        do {
            let data = try JSONEncoder().encode(result)
            if let resultFile {
                try data.write(to: URL(fileURLWithPath: resultFile), options: .atomic)
            } else {
                FileHandle.standardOutput.write(data + Data([10]))
            }
        } catch {
            FileHandle.standardError.write(Data("无法生成日历同步结果。\n".utf8))
            exit(1)
        }
        if !result.ok { exit(1) }
    }
}
