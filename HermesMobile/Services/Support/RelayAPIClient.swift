import Foundation

enum RelayCoders {
    private static func internetDateTimeStyle() -> Date.ISO8601FormatStyle {
        Date.ISO8601FormatStyle(timeZone: .gmt)
    }

    private static func internetDateTimeFractionalStyle() -> Date.ISO8601FormatStyle {
        Date.ISO8601FormatStyle(includingFractionalSeconds: true, timeZone: .gmt)
    }

    private static func normalizedRelayDateStrings(for value: String) -> [String] {
        if value.hasSuffix("Z") || value.range(of: #"[+-]\d{2}:\d{2}$"#, options: .regularExpression) != nil {
            return [value]
        }

        return ["\(value)Z"]
    }

    static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)

            if let date = parseRelayDate(value) {
                return date
            }

            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported relay date: \(value)"
            )
        }
        return decoder
    }

    static func parseRelayDate(_ value: String) -> Date? {
        for candidate in normalizedRelayDateStrings(for: value) {
            if let date = try? internetDateTimeFractionalStyle().parse(candidate) {
                return date
            }

            if let date = try? internetDateTimeStyle().parse(candidate) {
                return date
            }
        }

        return nil
    }
}

@MainActor
final class RelayAPIClient {
    private struct Envelope<T: Decodable>: Decodable {
        let data: T
    }

    private struct ErrorEnvelope: Decodable {
        struct ErrorPayload: Decodable {
            let code: String
            let message: String
            let retryable: Bool
        }

        let error: ErrorPayload
    }

    private struct FastAPIErrorEnvelope: Decodable {
        let detail: String
    }

    enum ClientError: LocalizedError {
        case unauthorized(String)
        case invalidURL(String)
        case requestFailed(String)

        var errorDescription: String? {
            switch self {
            case .unauthorized(let message):
                message
            case .invalidURL(let url):
                "Invalid relay URL: \(url)"
            case .requestFailed(let message):
                message
            }
        }
    }

    private let baseURLProvider: @MainActor () -> String
    private let session: URLSession
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(
        baseURLProvider: @escaping @MainActor () -> String,
        session: URLSession = .shared
    ) {
        self.baseURLProvider = baseURLProvider
        self.session = session
        self.encoder = RelayCoders.makeEncoder()
        self.decoder = RelayCoders.makeDecoder()
    }

    func get<T: Decodable>(
        path: String,
        accessToken: String? = nil
    ) async throws -> T {
        let request = try makeRequest(path: path, method: "GET", accessToken: accessToken, body: nil)
        return try await send(request)
    }

    func post<T: Decodable>(
        path: String,
        accessToken: String? = nil
    ) async throws -> T {
        let request = try makeRequest(path: path, method: "POST", accessToken: accessToken, body: nil)
        return try await send(request)
    }

    func post<Body: Encodable, T: Decodable>(
        path: String,
        body: Body,
        accessToken: String? = nil
    ) async throws -> T {
        let requestBody = try encoder.encode(body)
        let request = try makeRequest(
            path: path,
            method: "POST",
            accessToken: accessToken,
            body: requestBody
        )
        return try await send(request)
    }

    private func makeRequest(
        path: String,
        method: String,
        accessToken: String?,
        body: Data?
    ) throws -> URLRequest {
        let path = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let baseURLString = baseURLProvider().trimmingCharacters(in: CharacterSet(charactersIn: "/"))

        guard let url = URL(string: "\(baseURLString)/\(path)") else {
            throw ClientError.invalidURL(baseURLString)
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = body

        if let accessToken, !accessToken.isEmpty {
            request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        }

        return request
    }

    /// Opens an SSE stream to the given path and yields parsed events.
    ///
    /// The stream handles `event:` / `data:` lines per the SSE spec,
    /// ignores keepalive comments (lines starting with `:`), and
    /// terminates when the server closes the connection.
    nonisolated func streamEvents(
        path: String,
        accessToken: String?
    ) -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task { @MainActor in
                do {
                    var request = try makeRequest(
                        path: path,
                        method: "GET",
                        accessToken: accessToken,
                        body: nil
                    )
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.timeoutInterval = 300

                    let (bytes, response) = try await session.bytes(for: request)
                    let httpResponse = response as? HTTPURLResponse

                    guard let httpResponse, (200 ..< 300).contains(httpResponse.statusCode) else {
                        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                        if code == 401 {
                            continuation.finish(throwing: ClientError.unauthorized("Unauthorized"))
                        } else {
                            continuation.finish(throwing: ClientError.requestFailed(
                                "SSE stream failed with status \(code)."
                            ))
                        }
                        return
                    }

                    var currentEvent = "message"
                    var currentData = ""

                    for try await line in bytes.lines {
                        if Task.isCancelled { break }

                        // Keepalive comment
                        if line.hasPrefix(":") {
                            continue
                        }

                        // Empty line = dispatch event
                        if line.isEmpty {
                            if !currentData.isEmpty {
                                continuation.yield(SSEEvent(
                                    event: currentEvent,
                                    data: currentData
                                ))
                                currentEvent = "message"
                                currentData = ""
                            }
                            continue
                        }

                        if line.hasPrefix("event:") {
                            currentEvent = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces)
                        } else if line.hasPrefix("data:") {
                            let value = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
                            if currentData.isEmpty {
                                currentData = value
                            } else {
                                currentData += "\n" + value
                            }
                        }
                    }

                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }

            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await session.data(for: request)
        let httpResponse = response as? HTTPURLResponse

        guard let httpResponse else {
            throw ClientError.requestFailed("Relay returned an invalid response.")
        }

        guard (200 ..< 300).contains(httpResponse.statusCode) else {
            let makeError: (String) -> ClientError = { message in
                if httpResponse.statusCode == 401 {
                    return .unauthorized(message)
                }
                return .requestFailed(message)
            }

            if let errorEnvelope = try? decoder.decode(ErrorEnvelope.self, from: data) {
                throw makeError(errorEnvelope.error.message)
            }

            if let errorEnvelope = try? decoder.decode(FastAPIErrorEnvelope.self, from: data) {
                throw makeError(errorEnvelope.detail)
            }

            throw makeError("Relay request failed with status \(httpResponse.statusCode).")
        }

        return try decoder.decode(Envelope<T>.self, from: data).data
    }
}

// MARK: - DELETE and PATCH support

extension RelayAPIClient {
    func delete<T: Decodable>(
        path: String,
        accessToken: String? = nil
    ) async throws -> T {
        let request = try makeRequest(path: path, method: "DELETE", accessToken: accessToken, body: nil)
        return try await send(request)
    }

    func patch<Body: Encodable, T: Decodable>(
        path: String,
        body: Body,
        accessToken: String? = nil
    ) async throws -> T {
        let requestBody = try encoder.encode(body)
        let request = try makeRequest(
            path: path,
            method: "PATCH",
            accessToken: accessToken,
            body: requestBody
        )
        return try await send(request)
    }
}
