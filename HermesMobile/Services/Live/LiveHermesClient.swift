import Foundation
import os

@MainActor
final class LiveHermesClient: HermesClientProtocol {
    private static let logger = Logger(subsystem: "io.hermesmobile.HermesMobile", category: "LiveHermesClient")
    private static let maxRequestBodyBytes = 1_000_000
    private struct ConversationResponse: Decodable {
        let conversation: RelayConversation
    }

    private struct MessageResponse: Decodable {
        let replyState: String
        let conversation: RelayConversation
        let userMessage: RelayMessage?
        let message: RelayMessage?
        let jobId: UUID?
        let usage: TokenUsage?
        let diff: CodeDiff?
    }

    private struct RelayConversation: Decodable {
        let id: UUID
        let title: String
        let updatedAt: Date
        let messages: [RelayMessage]
        let latestUsage: TokenUsage?
    }

    private struct RelayAttachment: Decodable {
        let type: String
        let filename: String
        let mimeType: String
        let thumbnailData: String?
    }

    private struct RelayMessage: Decodable {
        let id: UUID
        let clientMessageId: UUID?
        let role: MessageSender
        let text: String
        let timestamp: Date
        let deliveryStatus: String?
        let jobId: UUID?
        let attachments: [RelayAttachment]?
    }

    private struct StreamProgressPayload: Decodable {
        let jobId: UUID?
        let kind: String?
        let delta: String?
        let label: String?
    }

    private struct StreamDonePayload: Decodable {
        let jobId: UUID?
        let status: String
        let usage: TokenUsage?
        let diff: CodeDiff?
        let error: String?
        let message: RelayMessage?
    }

    private struct AttachmentPayload: Encodable {
        let type: String    // "image" or "file"
        let filename: String
        let mimeType: String
        let data: String    // base64 encoded
        let thumbnailData: String?
    }

    private struct MessageCreateBody: Encodable {
        let conversationId: UUID?
        let text: String
        let clientMessageId: UUID
        let attachments: [AttachmentPayload]?
    }

    var connectionStatus: ConnectionStatus = .disconnected
    var currentConversation: Conversation?

    private let apiClient: RelayAPIClient
    private let accessTokenProvider: @MainActor () async -> String?
    private let accessTokenRefresher: @MainActor () async -> String?
    private let allowDemoFallback: Bool

    init(
        apiClient: RelayAPIClient,
        accessTokenProvider: @escaping @MainActor () async -> String?,
        accessTokenRefresher: @escaping @MainActor () async -> String? = { nil },
        allowDemoFallback: Bool = true
    ) {
        self.apiClient = apiClient
        self.accessTokenProvider = accessTokenProvider
        self.accessTokenRefresher = accessTokenRefresher
        self.allowDemoFallback = allowDemoFallback
    }

    func connect() async {
        connectionStatus = .connecting
        do {
            let response: ConversationResponse = try await performAuthorizedRequest { [self] token in
                try await self.apiClient.get(
                    path: "conversations/current",
                    accessToken: token
                )
            }
            currentConversation = mapConversation(response.conversation)
            connectionStatus = .connected
        } catch {
            connectionStatus = .error
        }
    }

    func disconnect() async {
        connectionStatus = .disconnected
    }

    func send(message: String, attachments: [PendingAttachment] = [], clientMessageID: UUID) async -> Message {
        do {
            let body = try self.makeCreateBody(
                text: message,
                attachments: attachments,
                clientMessageID: clientMessageID
            )
            let response: MessageResponse = try await performAuthorizedRequest { [self] token in
                try await self.apiClient.post(
                    path: "messages",
                    body: body,
                    accessToken: token
                )
            }
            currentConversation = mapConversation(response.conversation)
            connectionStatus = .connected
            if let message = response.message {
                return mapMessage(message)
            }
            if let userMessage = response.userMessage {
                return mapMessage(userMessage)
            }
            return Message(sender: .system, content: "Hermes did not return a message.", status: .failed)
        } catch {
            connectionStatus = .error
            return Message(sender: .system, content: failureMessage(for: error), status: .failed)
        }
    }

    func sendStreaming(message content: String, attachments: [PendingAttachment] = [], clientMessageID: UUID) -> AsyncStream<StreamingUpdate> {
        AsyncStream { continuation in
            Task { @MainActor [weak self] in
                guard let self else {
                    continuation.yield(.failed("Client deallocated"))
                    continuation.finish()
                    return
                }

                do {
                    let body = try self.makeCreateBody(
                        text: content,
                        attachments: attachments,
                        clientMessageID: clientMessageID
                    )
                    let response: MessageResponse = try await self.performAuthorizedRequest { [self] token in
                        try await self.apiClient.post(
                            path: "messages",
                            body: body,
                            accessToken: token
                        )
                    }

                    self.currentConversation = self.mapConversation(response.conversation)
                    self.connectionStatus = .connected

                    // If the reply is already complete (synchronous response), yield finished immediately
                    if response.replyState != "pending" {
                        if let msg = response.message {
                            continuation.yield(.finished(self.mapMessage(msg), response.usage, response.diff))
                        } else {
                            continuation.yield(.finished(
                                Message(sender: .system, content: "Hermes did not return a message.", status: .failed),
                                nil, nil
                            ))
                        }
                        continuation.finish()
                        return
                    }

                    // Reply is pending — stream job events via SSE
                    guard let jobId = response.jobId else {
                        // No jobId available, fall back to non-streaming result
                        if let msg = response.message ?? response.userMessage {
                            continuation.yield(.finished(self.mapMessage(msg), response.usage, response.diff))
                        } else {
                            continuation.yield(.finished(
                                Message(sender: .user, content: content, status: .sent),
                                nil, nil
                            ))
                        }
                        continuation.finish()
                        return
                    }

                    continuation.yield(.messageSent(jobID: jobId))

                    do {
                        let donePayload = try await self.streamJobEvents(jobId: jobId, continuation: continuation)
                        let refreshedConversation = await self.reloadConversationForStreaming()
                        let finalMessage = self.resolveFinalMessage(
                            jobId: jobId,
                            donePayload: donePayload,
                            conversation: refreshedConversation ?? self.currentConversation
                        )
                        continuation.yield(.finished(finalMessage, donePayload?.usage, donePayload?.diff))
                        continuation.finish()
                    } catch {
                        Self.logger.warning("SSE stream error: \(error.localizedDescription)")
                        continuation.yield(.failed("Stream interrupted"))
                        continuation.finish()
                    }

                } catch {
                    self.connectionStatus = .error
                    continuation.yield(.failed(self.failureMessage(for: error)))
                    continuation.finish()
                }
            }
        }
    }

    func loadConversation() async -> Conversation {
        do {
            let response: ConversationResponse = try await performAuthorizedRequest { [self] token in
                try await self.apiClient.get(
                    path: "conversations/current",
                    accessToken: token
                )
            }
            let conversation = mapConversation(response.conversation)
            currentConversation = conversation
            connectionStatus = .connected
            return conversation
        } catch {
            Self.logger.warning("Failed to load conversation from relay: \(error.localizedDescription)")
            connectionStatus = .error
            return currentConversation ?? fallbackConversation()
        }
    }

    func clearConversation() async throws -> Conversation {
        let response: ConversationResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.post(
                path: "conversations/current/clear",
                accessToken: token
            )
        }
        let conversation = mapConversation(response.conversation)
        currentConversation = conversation
        connectionStatus = .connected
        return conversation
    }

    func injectVoiceTranscript(voiceSessionId: UUID) async throws -> Conversation {
        let response: ConversationResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.post(
                path: "talk/session/\(voiceSessionId.uuidString.lowercased())/inject",
                accessToken: token
            )
        }
        let conversation = mapConversation(response.conversation)
        currentConversation = conversation
        return conversation
    }

    private func makeCreateBody(
        text: String,
        attachments: [PendingAttachment],
        clientMessageID: UUID
    ) throws -> MessageCreateBody {
        let payloads: [AttachmentPayload]? = attachments.isEmpty ? nil : attachments.map { att in
            AttachmentPayload(
                type: att.kind.rawValue,
                filename: att.fileName,
                mimeType: att.mimeType,
                data: att.base64Data,
                thumbnailData: att.thumbnailBase64
            )
        }
        let body = MessageCreateBody(
            conversationId: currentConversation?.id,
            text: text,
            clientMessageId: clientMessageID,
            attachments: payloads
        )
        try validateRequestBodySize(for: body)
        return body
    }

    private func fallbackConversation() -> Conversation {
        if allowDemoFallback {
            return DemoData.sampleConversation
        }

        return Conversation(title: "Hermes")
    }

    private func mapConversation(_ relayConversation: RelayConversation) -> Conversation {
        Conversation(
            id: relayConversation.id,
            title: relayConversation.title,
            messages: relayConversation.messages.map(mapMessage),
            lastActivity: relayConversation.updatedAt,
            latestUsage: relayConversation.latestUsage
        )
    }

    private func mapMessage(_ relayMessage: RelayMessage) -> Message {
        let attachments: [MessageAttachment] = (relayMessage.attachments ?? []).map { att in
            MessageAttachment(
                kind: att.type,
                fileName: att.filename,
                mimeType: att.mimeType,
                thumbnailBase64: att.thumbnailData
            )
        }
        return Message(
            id: relayMessage.id,
            clientMessageID: relayMessage.clientMessageId,
            sender: relayMessage.role,
            content: relayMessage.text,
            timestamp: relayMessage.timestamp,
            jobID: relayMessage.jobId,
            status: mapDeliveryStatus(relayMessage.deliveryStatus, sender: relayMessage.role),
            attachments: attachments
        )
    }

    private func mapDeliveryStatus(_ deliveryStatus: String?, sender: MessageSender) -> MessageStatus {
        switch deliveryStatus {
        case "pending":
            return .sending
        case "sent":
            return .sent
        case "delivered":
            return .delivered
        case "failed":
            return .failed
        default:
            return sender == .user ? .sent : .delivered
        }
    }

    private func performAuthorizedRequest<T>(
        _ operation: @escaping @MainActor (_ accessToken: String?) async throws -> T
    ) async throws -> T {
        do {
            return try await operation(await accessTokenProvider())
        } catch RelayAPIClient.ClientError.unauthorized {
            guard let refreshedToken = await accessTokenRefresher(), !refreshedToken.isEmpty else {
                throw RelayAPIClient.ClientError.unauthorized("Expired or invalid access token.")
            }
            return try await operation(refreshedToken)
        }
    }

    private func streamJobEvents(
        jobId: UUID,
        continuation: AsyncStream<StreamingUpdate>.Continuation
    ) async throws -> StreamDonePayload? {
        var didRetryUnauthorized = false
        var overrideToken: String?

        while true {
            let accessToken: String?
            if let override = overrideToken {
                accessToken = override
                overrideToken = nil
            } else {
                accessToken = await accessTokenProvider()
            }
            do {
                let stream = apiClient.streamEvents(
                    path: "jobs/\(jobId.uuidString.lowercased())/events",
                    accessToken: accessToken
                )
                for try await sseEvent in stream {
                    if Task.isCancelled { return nil }

                    switch sseEvent.event {
                    case "text_delta":
                        if let payload = decode(StreamProgressPayload.self, from: sseEvent.data),
                           let delta = payload.delta,
                           !delta.isEmpty {
                            continuation.yield(.textDelta(delta))
                        }
                    case "tool_activity":
                        if let payload = decode(StreamProgressPayload.self, from: sseEvent.data),
                           let label = payload.label,
                           !label.isEmpty {
                            continuation.yield(.toolActivity(label))
                        }
                    case "done":
                        return decode(StreamDonePayload.self, from: sseEvent.data)
                    default:
                        break
                    }
                }

                return nil
            } catch RelayAPIClient.ClientError.unauthorized {
                guard !didRetryUnauthorized else {
                    throw RelayAPIClient.ClientError.unauthorized("Expired or invalid access token.")
                }
                guard let refreshedToken = await accessTokenRefresher(), !refreshedToken.isEmpty else {
                    throw RelayAPIClient.ClientError.unauthorized("Expired or invalid access token.")
                }
                didRetryUnauthorized = true
                overrideToken = refreshedToken
                continue
            }
        }
    }

    private func reloadConversationForStreaming() async -> Conversation? {
        do {
            let response: ConversationResponse = try await performAuthorizedRequest { [self] token in
                try await self.apiClient.get(
                    path: "conversations/current",
                    accessToken: token
                )
            }
            let conversation = mapConversation(response.conversation)
            currentConversation = conversation
            connectionStatus = .connected
            return conversation
        } catch {
            Self.logger.warning("Failed to refresh conversation after streaming: \(error.localizedDescription)")
            return currentConversation
        }
    }

    private func resolveFinalMessage(
        jobId: UUID,
        donePayload: StreamDonePayload?,
        conversation: Conversation?
    ) -> Message {
        if let relayMessage = donePayload?.message {
            return mapMessage(relayMessage)
        }

        if let conversation,
           let message = conversation.messages.last(where: { $0.jobID == jobId && $0.sender != .user }) {
            return message
        }

        if donePayload?.status == "failed" {
            let rawError = donePayload?.error ?? ""
            let text: String
            if rawError.contains("413") || rawError.lowercased().contains("too large") {
                text = "The attachment was too large for Hermes to process. Try a smaller image."
            } else if rawError.isEmpty {
                text = "Hermes could not process this message."
            } else {
                // Strip URLs and technical details for a cleaner message
                let cleaned = rawError
                    .replacingOccurrences(of: #"For more information check: \S+"#, with: "", options: .regularExpression)
                    .replacingOccurrences(of: #"for url '\S+'"#, with: "", options: .regularExpression)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                text = "Hermes could not process this message: \(cleaned)"
            }
            return Message(sender: .system, content: text, jobID: jobId, status: .failed)
        }

        return Message(sender: .hermes, content: "", jobID: jobId, status: .delivered)
    }

    private func validateRequestBodySize(for body: MessageCreateBody) throws {
        let encoded = try RelayCoders.makeEncoder().encode(body)
        guard encoded.count <= Self.maxRequestBodyBytes else {
            throw RelayAPIClient.ClientError.requestFailed(
                "The attachment was too large for Hermes to process. Try a smaller image."
            )
        }
    }

    private func failureMessage(for error: Error) -> String {
        let rawError: String
        if let clientError = error as? RelayAPIClient.ClientError {
            rawError = clientError.errorDescription ?? error.localizedDescription
        } else {
            rawError = error.localizedDescription
        }

        if rawError.contains("413") || rawError.lowercased().contains("too large") {
            return "The attachment was too large for Hermes to process. Try a smaller image."
        }
        if rawError.isEmpty {
            return "Hermes relay is unavailable right now."
        }
        return rawError
    }

    private func decode<T: Decodable>(_ type: T.Type, from raw: String) -> T? {
        guard let data = raw.data(using: .utf8) else {
            Self.logger.warning("SSE decode: failed to convert raw string to UTF-8 data")
            return nil
        }
        do {
            return try RelayCoders.makeDecoder().decode(type, from: data)
        } catch {
            let snippet = String(raw.prefix(200))
            Self.logger.warning("SSE decode failed for \(String(describing: T.self)): \(error.localizedDescription) — raw: \(snippet)")
            return nil
        }
    }
}

// MARK: - Session Management

extension LiveHermesClient {
    private struct SessionListAPIResponse: Decodable {
        let sessions: [SessionAPIEntry]
        let total: Int
    }

    private struct SessionAPIEntry: Decodable {
        let id: UUID
        let title: String
        let previewText: String?
        let updatedAt: Date?
        let source: String?
        let isPinned: Bool?
        let isArchived: Bool?
    }

    private struct SessionAPIResponse: Decodable {
        let session: SessionAPIEntry
    }

    func listSessions(limit: Int, offset: Int) async throws -> SessionListResponse {
        let response: SessionListAPIResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.get(
                path: "sessions?limit=\(limit)&offset=\(offset)",
                accessToken: token
            )
        }
        let sessions = response.sessions.map { entry in
            SessionSummary(
                id: entry.id,
                title: entry.title,
                previewText: entry.previewText ?? "",
                lastActivity: entry.updatedAt ?? .now,
                source: entry.source,
                isPinned: entry.isPinned ?? false,
                isArchived: entry.isArchived ?? false
            )
        }
        return SessionListResponse(sessions: sessions, total: response.total)
    }

    func searchSessions(query: String) async throws -> [SessionSummary] {
        let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? query
        let response: SessionListAPIResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.get(
                path: "sessions/search?q=\(encoded)",
                accessToken: token
            )
        }
        return response.sessions.map { entry in
            SessionSummary(
                id: entry.id,
                title: entry.title,
                previewText: entry.previewText ?? "",
                lastActivity: entry.updatedAt ?? .now,
                source: entry.source,
                isPinned: entry.isPinned ?? false,
                isArchived: entry.isArchived ?? false
            )
        }
    }

    func createSession(title: String) async throws -> SessionSummary {
        struct CreateSessionBody: Encodable { let title: String }
        let response: SessionAPIResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.post(
                path: "sessions",
                body: CreateSessionBody(title: title),
                accessToken: token
            )
        }
        let entry = response.session
        return SessionSummary(
            id: entry.id,
            title: entry.title,
            previewText: entry.previewText ?? "",
            lastActivity: entry.updatedAt ?? .now,
            source: entry.source,
            isPinned: entry.isPinned ?? false,
            isArchived: entry.isArchived ?? false
        )
    }

    func deleteSession(id: UUID) async throws {
        struct EmptyResponse: Decodable {}
        let _: EmptyResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.delete(
                path: "sessions/\(id.uuidString.lowercased())",
                accessToken: token
            )
        }
    }

    func archiveSession(id: UUID) async throws {
        struct EmptyResponse: Decodable {}
        let _: EmptyResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.post(
                path: "sessions/\(id.uuidString.lowercased())/archive",
                accessToken: token
            )
        }
    }

    func togglePinSession(id: UUID) async throws -> SessionSummary {
        let response: SessionAPIResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.post(
                path: "sessions/\(id.uuidString.lowercased())/pin",
                accessToken: token
            )
        }
        let entry = response.session
        return SessionSummary(
            id: entry.id,
            title: entry.title,
            previewText: entry.previewText ?? "",
            lastActivity: entry.updatedAt ?? .now,
            source: entry.source,
            isPinned: entry.isPinned ?? false,
            isArchived: entry.isArchived ?? false
        )
    }

    func renameSession(id: UUID, title: String) async throws -> SessionSummary {
        struct RenameBody: Encodable { let title: String }
        let response: SessionAPIResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.patch(
                path: "sessions/\(id.uuidString.lowercased())",
                body: RenameBody(title: title),
                accessToken: token
            )
        }
        let entry = response.session
        return SessionSummary(
            id: entry.id,
            title: entry.title,
            previewText: entry.previewText ?? "",
            lastActivity: entry.updatedAt ?? .now,
            source: entry.source,
            isPinned: entry.isPinned ?? false,
            isArchived: entry.isArchived ?? false
        )
    }

    func loadConversation(id: UUID) async throws -> Conversation {
        let response: ConversationResponse = try await performAuthorizedRequest { [self] token in
            try await self.apiClient.get(
                path: "sessions/\(id.uuidString.lowercased())/conversation",
                accessToken: token
            )
        }
        let conversation = mapConversation(response.conversation)
        currentConversation = conversation
        connectionStatus = .connected
        return conversation
    }
}
