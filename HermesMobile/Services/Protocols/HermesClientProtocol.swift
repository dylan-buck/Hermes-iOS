import Foundation

/// Response from a paginated session list request.
struct SessionListResponse: Codable, Sendable {
    let sessions: [SessionSummary]
    let total: Int
}

@MainActor
protocol HermesClientProtocol {
    var connectionStatus: ConnectionStatus { get }
    var currentConversation: Conversation? { get }
    func connect() async
    func disconnect() async
    func send(message: String, attachments: [PendingAttachment], clientMessageID: UUID) async -> Message
    func sendStreaming(message: String, attachments: [PendingAttachment], clientMessageID: UUID) -> AsyncStream<StreamingUpdate>
    func loadConversation() async -> Conversation
    func clearConversation() async throws -> Conversation
    func injectVoiceTranscript(voiceSessionId: UUID) async throws -> Conversation

    // MARK: - Session Management

    /// List sessions with pagination.
    func listSessions(limit: Int, offset: Int) async throws -> SessionListResponse

    /// Search sessions by query string.
    func searchSessions(query: String) async throws -> [SessionSummary]

    /// Create a new session.
    func createSession(title: String) async throws -> SessionSummary

    /// Delete a session by ID.
    func deleteSession(id: UUID) async throws

    /// Archive a session by ID.
    func archiveSession(id: UUID) async throws

    /// Toggle pin state for a session.
    func togglePinSession(id: UUID) async throws -> SessionSummary

    /// Rename a session.
    func renameSession(id: UUID, title: String) async throws -> SessionSummary

    /// Load a specific conversation by session ID.
    func loadConversation(id: UUID) async throws -> Conversation
}
