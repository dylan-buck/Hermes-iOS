import Foundation

nonisolated enum DemoData {

    // MARK: - Chat

    static let sampleConversation: Conversation = {
        let cal = Calendar.current
        let now = Date.now
        return Conversation(
            title: "Hermes Agent",
            messages: [
                Message(
                    sender: .hermes,
                    content: "Good morning! I've been reviewing your schedule for today. You have a team standup at 10 AM and a design review at 2 PM. Would you like me to prepare any talking points?",
                    timestamp: cal.date(byAdding: .hour, value: -3, to: now) ?? now,
                    status: .delivered
                ),
                Message(
                    sender: .user,
                    content: "Yes please, especially for the design review. Can you pull together the latest mockups and any feedback from last week?",
                    timestamp: cal.date(byAdding: .hour, value: -3, to: now)?.addingTimeInterval(120) ?? now,
                    status: .delivered
                ),
                Message(
                    sender: .hermes,
                    content: "Done. I've compiled the latest Figma exports and summarized the three key feedback themes from last week's session: navigation clarity, color contrast accessibility, and onboarding flow length. I've also flagged two open questions from Elena's comments. Want me to create a brief deck?",
                    timestamp: cal.date(byAdding: .hour, value: -2, to: now) ?? now,
                    status: .delivered
                ),
                Message(
                    sender: .user,
                    content: "That would be great. Keep it to 5 slides max.",
                    timestamp: cal.date(byAdding: .hour, value: -2, to: now)?.addingTimeInterval(60) ?? now,
                    status: .delivered
                ),
                Message(
                    sender: .hermes,
                    content: "I'll have that ready within the hour. Also, I noticed your grocery delivery is scheduled for 4 PM today. Should I keep that or reschedule given the design review might run long?",
                    timestamp: cal.date(byAdding: .hour, value: -1, to: now) ?? now,
                    status: .delivered
                ),
                Message(
                    sender: .user,
                    content: "Good catch. Push it to 6 PM if possible.",
                    timestamp: cal.date(byAdding: .minute, value: -45, to: now) ?? now,
                    status: .delivered
                ),
                Message(
                    sender: .hermes,
                    content: "Rescheduled to 6 PM. Your delivery window is now 6:00-6:30 PM. I'll send you a reminder at 5:45. Is there anything else you'd like me to handle before the standup?",
                    timestamp: cal.date(byAdding: .minute, value: -30, to: now) ?? now,
                    status: .delivered
                ),
            ],
            lastActivity: cal.date(byAdding: .minute, value: -30, to: now) ?? now
        )
    }()

    // MARK: - Inbox

    static let sampleInboxItems: [InboxItem] = {
        let cal = Calendar.current
        let now = Date.now
        return [
            InboxItem(
                serverID: UUID(),
                type: .approval,
                title: "Grocery Delivery Reschedule",
                body: "Confirm moving your delivery from 4 PM to 6 PM today. The store has confirmed the new window.",
                timestamp: cal.date(byAdding: .minute, value: -25, to: now) ?? now,
                priority: .high,
                primaryAction: InboxActionDescriptor(id: "approve", title: "Approve"),
                secondaryAction: InboxActionDescriptor(id: "dismiss", title: "Dismiss", isDestructive: true)
            ),
            InboxItem(
                serverID: UUID(),
                type: .suggestion,
                title: "Weekend Trip Planning",
                body: "Based on the weather forecast, Saturday looks ideal for the hike you mentioned. I've found 3 trails within an hour's drive.",
                timestamp: cal.date(byAdding: .hour, value: -2, to: now) ?? now,
                primaryAction: InboxActionDescriptor(id: "open", title: "Review"),
                secondaryAction: InboxActionDescriptor(id: "dismiss", title: "Dismiss", isDestructive: true)
            ),
            InboxItem(
                serverID: UUID(),
                type: .reminder,
                title: "Design Review Prep",
                body: "Your presentation deck is ready. 5 slides covering navigation, accessibility, and onboarding feedback. Review before 2 PM.",
                timestamp: cal.date(byAdding: .hour, value: -1, to: now) ?? now,
                primaryAction: InboxActionDescriptor(id: "open", title: "Open"),
                secondaryAction: InboxActionDescriptor(id: "dismiss", title: "Dismiss", isDestructive: true)
            ),
            InboxItem(
                serverID: UUID(),
                type: .notification,
                title: "Calendar Update",
                body: "Elena added a comment to tomorrow's brainstorm session: 'Let's also discuss the new component library.'",
                timestamp: cal.date(byAdding: .hour, value: -4, to: now) ?? now,
                isRead: true,
                status: .opened,
                primaryAction: InboxActionDescriptor(id: "open", title: "Open"),
                secondaryAction: InboxActionDescriptor(id: "dismiss", title: "Dismiss", isDestructive: true)
            ),
            InboxItem(
                serverID: UUID(),
                type: .alert,
                title: "Unusual Login Detected",
                body: "A new device accessed your connected email account from Portland, OR. If this wasn't you, I recommend changing your password.",
                timestamp: cal.date(byAdding: .hour, value: -6, to: now) ?? now,
                priority: .urgent,
                primaryAction: InboxActionDescriptor(id: "open", title: "Review"),
                secondaryAction: InboxActionDescriptor(id: "dismiss", title: "Dismiss", isDestructive: true)
            ),
            InboxItem(
                serverID: UUID(),
                type: .suggestion,
                title: "Reading Recommendation",
                body: "Based on your recent interest in design systems, I found an article: 'Building Resilient Component Libraries at Scale' by Ethan Marcotte.",
                timestamp: cal.date(byAdding: .hour, value: -8, to: now) ?? now,
                isRead: true,
                isActionable: false,
                status: .opened
            ),
            InboxItem(
                serverID: UUID(),
                type: .approval,
                title: "Smart Home Scene",
                body: "Ready to activate 'Evening Wind Down' scene at 9 PM tonight: dim lights to 30%, set thermostat to 68F, play ambient playlist.",
                timestamp: cal.date(byAdding: .day, value: -1, to: now) ?? now,
                isRead: true,
                status: .completed
            ),
            InboxItem(
                serverID: UUID(),
                type: .reminder,
                title: "Water Plants",
                body: "Your monstera and fiddle leaf fig are due for watering today based on the schedule you set up last month.",
                timestamp: cal.date(byAdding: .day, value: -1, to: now)?.addingTimeInterval(3600) ?? now,
                isRead: true,
                isActionable: false,
                status: .dismissed
            ),
        ]
    }()

    // MARK: - Permissions

    static let sampleCapabilities: [DeviceCapability] = [
        DeviceCapability(
            permissionType: .location,
            status: .authorizedWhenInUse,
            statusDetail: "While Using \u{2022} Full Accuracy"
        ),
        DeviceCapability(permissionType: .health, status: .notDetermined),
        DeviceCapability(permissionType: .notifications, status: .authorized),
        DeviceCapability(permissionType: .camera, status: .notDetermined),
        DeviceCapability(permissionType: .photos, status: .denied),
    ]

    // MARK: - Settings

    static let sampleUserSettings = UserSettings(
        userName: "Alex",
        avatarInitials: "A",
        notificationsEnabled: true,
        hapticFeedbackEnabled: true,
        environment: AppEnvironmentPolicy.currentBuild.defaultEnvironment,
        autoConnectOnLaunch: true
    )

    // MARK: - Sessions

    static let sampleSessions: [SessionSummary] = {
        let cal = Calendar.current
        let now = Date.now
        return [
            SessionSummary(
                title: "Docker compose setup",
                previewText: "I've fixed the nginx config and updated the compose file...",
                lastActivity: cal.date(byAdding: .minute, value: -15, to: now) ?? now,
                source: "cli",
                isPinned: true
            ),
            SessionSummary(
                title: "Design review prep",
                previewText: "Your 5-slide deck is ready for the 2 PM review.",
                lastActivity: cal.date(byAdding: .hour, value: -1, to: now) ?? now,
                source: "ios",
                isPinned: true
            ),
            SessionSummary(
                title: "Morning briefing",
                previewText: "Good morning! Here's your schedule and priorities...",
                lastActivity: cal.date(byAdding: .hour, value: -3, to: now) ?? now,
                source: "hermes-ios"
            ),
            SessionSummary(
                title: "Grocery list update",
                previewText: "Added oat milk, avocados, and sourdough bread.",
                lastActivity: cal.date(byAdding: .hour, value: -5, to: now) ?? now,
                source: "imessage"
            ),
            SessionSummary(
                title: "Smart home automations",
                previewText: "Created evening routine: lights dim at 9 PM...",
                lastActivity: cal.date(byAdding: .day, value: -1, to: now) ?? now,
                source: "web"
            ),
            SessionSummary(
                title: "Weekend trip planning",
                previewText: "Found 3 great hiking trails within an hour's drive.",
                lastActivity: cal.date(byAdding: .day, value: -1, to: now)?.addingTimeInterval(7200) ?? now,
                source: "telegram"
            ),
            SessionSummary(
                title: "Python script debugging",
                previewText: "The issue was a missing async context manager...",
                lastActivity: cal.date(byAdding: .day, value: -2, to: now) ?? now,
                source: "cli"
            ),
        ]
    }()
}
