import Foundation
import Combine

@MainActor
@Observable
final class SessionListStore {
    var pinnedSessions: [SessionSummary] = []
    var recentSessions: [SessionSummary] = []
    var searchResults: [SessionSummary]?
    var isLoading = false
    var searchQuery = ""
    var errorMessage: String?

    /// Total session count from last fetch (for pagination).
    private var totalCount = 0
    /// Current page offset for pagination.
    private var currentOffset = 0
    /// Page size for load-more pagination.
    private let pageSize = 50
    /// Whether more sessions are available to load.
    var hasMore: Bool { currentOffset < totalCount }

    private let hermesClient: any HermesClientProtocol
    private let chatStore: ChatStore
    private var searchTask: Task<Void, Never>?
    private var searchObservationTask: Task<Void, Never>?

    init(hermesClient: any HermesClientProtocol, chatStore: ChatStore) {
        self.hermesClient = hermesClient
        self.chatStore = chatStore
        observeSearchQuery()
    }

    // MARK: - Load Sessions

    func loadSessions() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        do {
            let response = try await hermesClient.listSessions(limit: pageSize, offset: 0)
            currentOffset = response.sessions.count
            totalCount = response.total
            splitSessions(response.sessions)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func loadMore() async {
        guard hasMore, !isLoading else { return }
        isLoading = true
        defer { isLoading = false }

        do {
            let response = try await hermesClient.listSessions(limit: pageSize, offset: currentOffset)
            currentOffset += response.sessions.count
            // Merge new sessions and re-split
            let allSessions = pinnedSessions + recentSessions + response.sessions
            splitSessions(allSessions)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - Search

    func search() async {
        let query = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            searchResults = nil
            return
        }

        do {
            let results = try await hermesClient.searchSessions(query: query)
            searchResults = results
        } catch {
            errorMessage = error.localizedDescription
            searchResults = []
        }
    }

    // MARK: - Session Actions

    func createNewSession(title: String = "New Chat") async {
        do {
            let session = try await hermesClient.createSession(title: title)
            recentSessions.insert(session, at: 0)
            await switchToSession(session)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func switchToSession(_ session: SessionSummary) async {
        do {
            let conversation = try await hermesClient.loadConversation(id: session.id)
            chatStore.conversation = conversation
            if let latestUsage = conversation.latestUsage {
                chatStore.lastTokenUsage = latestUsage
            }
            chatStore.onConversationChanged?()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func deleteSession(_ session: SessionSummary) async {
        do {
            try await hermesClient.deleteSession(id: session.id)
            pinnedSessions.removeAll { $0.id == session.id }
            recentSessions.removeAll { $0.id == session.id }
            searchResults?.removeAll { $0.id == session.id }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func archiveSession(_ session: SessionSummary) async {
        do {
            try await hermesClient.archiveSession(id: session.id)
            pinnedSessions.removeAll { $0.id == session.id }
            recentSessions.removeAll { $0.id == session.id }
            searchResults?.removeAll { $0.id == session.id }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func togglePin(_ session: SessionSummary) async {
        do {
            let updated = try await hermesClient.togglePinSession(id: session.id)
            // Remove from current location
            pinnedSessions.removeAll { $0.id == session.id }
            recentSessions.removeAll { $0.id == session.id }
            // Re-insert in correct bucket
            if updated.isPinned {
                pinnedSessions.append(updated)
                pinnedSessions.sort { $0.lastActivity > $1.lastActivity }
            } else {
                recentSessions.append(updated)
                recentSessions.sort { $0.lastActivity > $1.lastActivity }
            }
            // Update search results if visible
            if let idx = searchResults?.firstIndex(where: { $0.id == session.id }) {
                searchResults?[idx] = updated
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func renameSession(_ session: SessionSummary, newTitle: String) async {
        do {
            let updated = try await hermesClient.renameSession(id: session.id, title: newTitle)
            if let idx = pinnedSessions.firstIndex(where: { $0.id == session.id }) {
                pinnedSessions[idx] = updated
            }
            if let idx = recentSessions.firstIndex(where: { $0.id == session.id }) {
                recentSessions[idx] = updated
            }
            if let idx = searchResults?.firstIndex(where: { $0.id == session.id }) {
                searchResults?[idx] = updated
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - Helpers

    /// The active conversation's session ID, if any.
    var activeSessionID: UUID? {
        chatStore.conversation?.id
    }

    /// All sessions grouped by source for platform sub-sections.
    var sessionsBySource: [(source: String, sessions: [SessionSummary])] {
        let all = recentSessions
        let grouped = Dictionary(grouping: all) { $0.source ?? "hermes" }
        return grouped
            .sorted { $0.key < $1.key }
            .map { (source: $0.key, sessions: $0.value) }
    }

    func reset() {
        pinnedSessions = []
        recentSessions = []
        searchResults = nil
        isLoading = false
        searchQuery = ""
        errorMessage = nil
        totalCount = 0
        currentOffset = 0
        searchTask?.cancel()
        searchTask = nil
    }

    // MARK: - Private

    private func splitSessions(_ sessions: [SessionSummary]) {
        let nonArchived = sessions.filter { !$0.isArchived }
        pinnedSessions = nonArchived.filter(\.isPinned).sorted { $0.lastActivity > $1.lastActivity }
        recentSessions = nonArchived.filter { !$0.isPinned }.sorted { $0.lastActivity > $1.lastActivity }
    }

    private func observeSearchQuery() {
        searchObservationTask = Task { [weak self] in
            guard let self else { return }
            var lastQuery = ""
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(100))
                let current = self.searchQuery
                guard current != lastQuery else { continue }
                lastQuery = current
                // Debounce 300ms
                try? await Task.sleep(for: .milliseconds(300))
                guard !Task.isCancelled, self.searchQuery == current else { continue }
                await self.search()
            }
        }
    }
}
