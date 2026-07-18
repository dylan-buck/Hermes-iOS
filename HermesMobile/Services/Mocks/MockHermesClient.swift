import Foundation

@MainActor
@Observable
final class MockHermesClient: HermesClientProtocol {
    var connectionStatus: ConnectionStatus = .connected
    var currentConversation: Conversation?

    private let responseDelay: TimeInterval = 1.5

    func connect() async {
        connectionStatus = .connecting
        try? await Task.sleep(for: .seconds(0.8))
        connectionStatus = .connected
    }

    func disconnect() async {
        connectionStatus = .disconnected
    }

    func send(message content: String, attachments: [PendingAttachment] = [], clientMessageID: UUID) async -> Message {
        let userMessage = Message(
            clientMessageID: clientMessageID,
            sender: .user,
            content: content,
            status: .sent,
            attachments: attachments.map { MessageAttachment(from: $0) }
        )

        currentConversation?.messages.append(userMessage)

        // Simulate Hermes thinking and responding
        try? await Task.sleep(for: .seconds(responseDelay))

        let response = generateResponse(for: content)
        let hermesMessage = Message(
            sender: .hermes,
            content: response,
            status: .delivered
        )

        currentConversation?.messages.append(hermesMessage)

        return hermesMessage
    }

    func sendStreaming(message content: String, attachments: [PendingAttachment] = [], clientMessageID: UUID) -> AsyncStream<StreamingUpdate> {
        AsyncStream { continuation in
            Task { @MainActor [weak self] in
                guard let self else {
                    continuation.finish()
                    return
                }

                let userMessage = Message(
                    clientMessageID: clientMessageID,
                    sender: .user,
                    content: content,
                    status: .sent,
                    attachments: attachments.map { MessageAttachment(from: $0) }
                )
                self.currentConversation?.messages.append(userMessage)

                continuation.yield(.messageSent(jobID: UUID()))

                // Simulate tool activity
                try? await Task.sleep(for: .seconds(0.5))
                continuation.yield(.toolActivity("Searching..."))

                // Simulate streaming text
                try? await Task.sleep(for: .seconds(0.3))
                let response = self.generateResponse(for: content)
                let words = response.split(separator: " ")
                for word in words {
                    try? await Task.sleep(for: .milliseconds(50))
                    continuation.yield(.textDelta(String(word) + " "))
                }

                let hermesMessage = Message(
                    sender: .hermes,
                    content: response,
                    status: .delivered
                )
                self.currentConversation?.messages.append(hermesMessage)

                continuation.yield(.finished(
                    hermesMessage,
                    TokenUsage(promptTokens: 150, completionTokens: 80, totalTokens: 230),
                    nil
                ))
                continuation.finish()
            }
        }
    }

    func loadConversation() async -> Conversation {
        let conversation = DemoData.sampleConversation
        currentConversation = conversation
        return conversation
    }

    func clearConversation() async throws -> Conversation {
        let fresh = Conversation(title: "Hermes")
        currentConversation = fresh
        return fresh
    }

    func injectVoiceTranscript(voiceSessionId: UUID) async throws -> Conversation {
        return currentConversation ?? Conversation(title: "Hermes")
    }

    private func generateResponse(for input: String) -> String {
        let responses = [
            "I've looked into that for you. Based on what I can see, here's what I'd suggest...",
            "That's a great question. Let me break it down for you step by step.",
            "I've been thinking about this. Here are a few options we could consider.",
            "Understood. I'll take care of that right away. Is there anything else you need?",
            "I found some interesting information about that. Let me share what I've gathered.",
            "Good thinking. I've already started working on it. You should see results shortly.",
        ]
        return responses.randomElement() ?? responses[0]
    }
}

// MARK: - Session Management (Mock)

extension MockHermesClient {
    func listSessions(limit: Int, offset: Int) async throws -> SessionListResponse {
        let all = DemoData.sampleSessions
        let page = Array(all.dropFirst(offset).prefix(limit))
        return SessionListResponse(sessions: page, total: all.count)
    }

    func searchSessions(query: String) async throws -> [SessionSummary] {
        let q = query.lowercased()
        return DemoData.sampleSessions.filter {
            $0.title.lowercased().contains(q) || $0.previewText.lowercased().contains(q)
        }
    }

    func createSession(title: String) async throws -> SessionSummary {
        SessionSummary(title: title, previewText: "New conversation", source: "ios")
    }

    func deleteSession(id: UUID) async throws {}

    func archiveSession(id: UUID) async throws {}

    func togglePinSession(id: UUID) async throws -> SessionSummary {
        SessionSummary(id: id, title: "Pinned", isPinned: true)
    }

    func renameSession(id: UUID, title: String) async throws -> SessionSummary {
        SessionSummary(id: id, title: title)
    }

    func loadConversation(id: UUID) async throws -> Conversation {
        currentConversation ?? DemoData.sampleConversation
    }
}
