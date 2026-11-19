import AVFoundation
import Foundation
import Speech

@MainActor
@Observable
final class PermissionsStore {
    var capabilities: [DeviceCapability] = []

    private let locationService: any LocationServiceProtocol
    private let healthService: any HealthServiceProtocol
    private let notificationService: any NotificationServiceProtocol
    private let mediaService: any MediaServiceProtocol
    private let motionService: LiveMotionService?

    init(
        locationService: any LocationServiceProtocol,
        healthService: any HealthServiceProtocol,
        notificationService: any NotificationServiceProtocol,
        mediaService: any MediaServiceProtocol,
        motionService: LiveMotionService? = nil
    ) {
        self.locationService = locationService
        self.healthService = healthService
        self.notificationService = notificationService
        self.mediaService = mediaService
        self.motionService = motionService
        self.capabilities = currentCapabilities()
    }

    func reloadCapabilities() async {
        locationService.refreshAuthorizationState()
        await healthService.refreshAuthorizationStatus()
        await notificationService.refreshAuthorizationStatus()
        capabilities = currentCapabilities()
    }

    func requestPermission(for type: PermissionType) async {
        switch type {
        case .location:
            _ = await locationService.requestAuthorization()
        case .health:
            _ = await healthService.requestAuthorization()
        case .notifications:
            _ = await notificationService.requestAuthorization()
        case .microphone:
            await requestMicrophoneAuthorization()
        case .camera:
            _ = await mediaService.requestCameraAuthorization()
        case .photos:
            _ = await mediaService.requestPhotosAuthorization()
        case .motion:
            _ = await motionService?.requestAuthorization()
        case .speechRecognition:
            await requestSpeechAuthorization()
        }

        capabilities = currentCapabilities()
    }

    var locationAuthorizationLevel: LocationAuthorizationLevel {
        locationService.authorizationLevel
    }

    var locationAccuracyLevel: LocationAccuracyLevel {
        locationService.accuracyLevel
    }

    var healthBackgroundDeliveryEnabled: Bool {
        healthService.backgroundDeliveryEnabled
    }

    func requestBackgroundLocationAccess() async {
        _ = await locationService.requestBackgroundAuthorization()
        capabilities = currentCapabilities()
    }

    func updateLocationSyncPreference(_ preference: LocationSyncPreference) {
        locationService.updateSyncPreference(preference)
        capabilities = currentCapabilities()
    }

    func openLocationSystemSettings() {
        locationService.openSystemSettings()
    }

    private func currentCapabilities() -> [DeviceCapability] {
        [
            DeviceCapability(
                permissionType: .location,
                status: locationService.authorizationStatus,
                statusDetail: locationStatusDetail()
            ),
            DeviceCapability(
                permissionType: .health,
                status: healthService.authorizationStatus,
                statusDetail: healthStatusDetail()
            ),
            DeviceCapability(permissionType: .notifications, status: notificationService.authorizationStatus),
            DeviceCapability(permissionType: .microphone, status: microphoneAuthorizationStatus()),
            DeviceCapability(permissionType: .camera, status: mediaService.cameraAuthorizationStatus),
            DeviceCapability(permissionType: .photos, status: mediaService.photosAuthorizationStatus),
            DeviceCapability(permissionType: .motion, status: motionService?.authorizationStatus ?? .unsupported),
            DeviceCapability(permissionType: .speechRecognition, status: speechRecognitionStatus()),
        ]
    }

    // MARK: - Microphone

    private func microphoneAuthorizationStatus() -> PermissionStatus {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: .authorized
        case .denied: .denied
        case .undetermined: .notDetermined
        @unknown default: .notDetermined
        }
    }

    private func requestMicrophoneAuthorization() async {
        guard AVAudioApplication.shared.recordPermission == .undetermined else { return }
        _ = await AVAudioApplication.requestRecordPermission()
    }

    // MARK: - Speech Recognition

    private func speechRecognitionStatus() -> PermissionStatus {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: .authorized
        case .denied: .denied
        case .restricted: .restricted
        case .notDetermined: .notDetermined
        @unknown default: .notDetermined
        }
    }

    private func requestSpeechAuthorization() async {
        guard SFSpeechRecognizer.authorizationStatus() == .notDetermined else { return }
        await withCheckedContinuation { continuation in
            // TCC calls this on a background queue. Without @Sendable the closure
            // inherits this type's @MainActor isolation, and Swift 6's runtime
            // isolation check traps when it runs.
            SFSpeechRecognizer.requestAuthorization { @Sendable _ in
                continuation.resume()
            }
        }
    }

    private func locationStatusDetail() -> String? {
        switch locationService.authorizationLevel {
        case .whenInUse, .always:
            return "\(locationService.authorizationLevel.displayLabel) • \(locationService.accuracyLevel.displayLabel)"
        case .notDetermined, .denied, .restricted:
            return nil
        }
    }

    private func healthStatusDetail() -> String? {
        switch healthService.authorizationStatus {
        case .authorized:
            let backgroundStatus = healthService.backgroundDeliveryEnabled ? "Background Sync On" : "Background Sync Off"
            return "Read Only • \(backgroundStatus)"
        case .denied, .restricted:
            return "Manage in Apple Health or Settings > Privacy & Security > Health"
        default:
            return nil
        }
    }
}
