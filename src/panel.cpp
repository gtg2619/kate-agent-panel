#include "panel.h"
#include "filelink.h"
#include <KLocalizedString>
#include <KTextEditor/Document>
#include <KTextEditor/MainWindow>
#include <KTextEditor/View>
#include <QComboBox>
#include <QApplication>
#include <QClipboard>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDateTime>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QEvent>
#include <QFontDatabase>
#include <QFontInfo>
#include <QFontMetrics>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QInputMethodEvent>
#include <QJsonDocument>
#include <QLabel>
#include <QKeyEvent>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QShortcut>
#include <QStandardItemModel>
#include <QStandardPaths>
#include <QStyle>
#include <QTimer>
#include <QTextBlock>
#include <QTextDocument>
#include <QTextLayout>
#include <QTreeWidget>
#include <QToolButton>
#include <QUuid>
#include <QVBoxLayout>
#include <QWebChannel>
#include <QWebEnginePage>
#include <QWebEngineProfile>
#include <QWebEngineSettings>
#include <QWebEngineUrlRequestInfo>
#include <QWebEngineUrlRequestInterceptor>
#include <QWebEngineView>

namespace {
class OfflineRequests : public QWebEngineUrlRequestInterceptor
{
public:
    using QWebEngineUrlRequestInterceptor::QWebEngineUrlRequestInterceptor;
    void interceptRequest(QWebEngineUrlRequestInfo &info) override
    {
        const auto scheme = info.requestUrl().scheme();
        info.block(scheme != QLatin1String("qrc") && scheme != QLatin1String("data"));
    }
};

class TranscriptPage : public QWebEnginePage
{
public:
    explicit TranscriptPage(QWebEngineProfile *profile, AgentPanel *panel, QObject *parent)
        : QWebEnginePage(profile, parent), m_panel(panel) {}
    bool acceptNavigationRequest(const QUrl &url, NavigationType type, bool mainFrame) override
    {
        if (type == NavigationTypeLinkClicked) {
            m_panel->openLink(url.toString());
            return false;
        }
        return !mainFrame || url == QUrl(QStringLiteral("qrc:/agentpanel/web/index.html"));
    }
    QWebEnginePage *createWindow(WebWindowType) override { return nullptr; }
private:
    AgentPanel *m_panel;
};
}

AgentPanel::AgentPanel(KTextEditor::MainWindow *window, QWidget *parent)
    : QWidget(parent), m_window(window), m_process(new QProcess(this))
{
    setObjectName(QStringLiteral("kateAgentPanel"));
    auto outer = new QVBoxLayout(this);
    outer->setContentsMargins(4, 4, 4, 4);
    outer->setSpacing(4);
    auto toolbar = new QHBoxLayout;
    m_agents = new QComboBox(this);
    m_agents->setAccessibleName(i18n("Agent"));
    m_agents->setMinimumContentsLength(12);
    m_agents->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    toolbar->addWidget(m_agents);
    m_workspace = new QLineEdit(inferWorkspace(), this);
    m_workspace->setObjectName(QStringLiteral("agentWorkspace"));
    m_workspace->setAccessibleName(i18n("Working directory"));
    m_workspace->setToolTip(i18n("Working directory for this conversation"));
    toolbar->addWidget(m_workspace, 1);
    m_model = new QLineEdit(this);
    m_model->setObjectName(QStringLiteral("agentModelId"));
    m_model->setAccessibleName(i18n("Model ID"));
    m_model->setPlaceholderText(i18n("Model ID"));
    m_model->setToolTip(i18n("Effective CLI model. Changes apply to this session when you finish editing."));
    m_model->setMaximumWidth(190);
    toolbar->addWidget(m_model);
    m_effort = new QLineEdit(this);
    m_effort->setObjectName(QStringLiteral("agentReasoningEffort"));
    m_effort->setAccessibleName(i18n("Reasoning effort"));
    m_effort->setPlaceholderText(i18n("Reasoning effort"));
    m_effort->setToolTip(i18n("Effective CLI reasoning effort. Changes apply to this session when you finish editing."));
    m_effort->setMaximumWidth(140);
    toolbar->addWidget(m_effort);
    auto toolButton = [this](const QString &icon, const QString &label) {
        auto button = new QToolButton(this);
        button->setIcon(QIcon::fromTheme(icon));
        button->setText(label);
        button->setToolTip(label);
        button->setAccessibleName(label);
        button->setAutoRaise(true);
        return button;
    };
    auto browse = toolButton(QStringLiteral("document-open-folder"), i18n("Choose Working Directory"));
    toolbar->addWidget(browse);
    auto refresh = toolButton(QStringLiteral("view-refresh"), i18n("Refresh Agents"));
    refresh->setToolTip(i18n("Check executable paths and environment variable names"));
    toolbar->addWidget(refresh);
    m_sessions = toolButton(QStringLiteral("view-history"), i18n("Local Sessions"));
    m_sessions->setObjectName(QStringLiteral("agentLocalSessions"));
    toolbar->addWidget(m_sessions);
    connect(m_sessions, &QToolButton::clicked, this, &AgentPanel::showSessions);
    m_new = toolButton(QStringLiteral("document-new"), i18n("New Chat"));
    m_new->setObjectName(QStringLiteral("agentNewChat"));
    toolbar->addWidget(m_new);
    m_restore = toolButton(QStringLiteral("edit-undo"), i18n("Restore last input"));
    m_restore->setObjectName(QStringLiteral("agentRestoreInput"));
    m_restore->hide();
    toolbar->addWidget(m_restore);
    outer->addLayout(toolbar);

    // An off-the-record profile, local-only requests, and HTML-disabled Markdown keep
    // untrusted model output out of the native bridge's execution surface.
    auto profile = new QWebEngineProfile(this);
    auto interceptor = new OfflineRequests(profile);
    profile->setUrlRequestInterceptor(interceptor);
    m_transcript = new QWebEngineView(this);
    m_transcript->setMinimumHeight(48);
    auto page = new TranscriptPage(profile, this, m_transcript);
    m_transcript->setPage(page);
    updateTheme();
    connect(m_transcript, &QObject::destroyed, profile, &QObject::deleteLater);
    page->settings()->setAttribute(QWebEngineSettings::LocalContentCanAccessRemoteUrls, false);
    page->settings()->setAttribute(QWebEngineSettings::LocalContentCanAccessFileUrls, false);
    page->settings()->setAttribute(QWebEngineSettings::JavascriptCanOpenWindows, false);
    auto channel = new QWebChannel(page);
    channel->registerObject(QStringLiteral("panel"), new TranscriptBridge(this));
    page->setWebChannel(channel);
    m_transcript->load(QUrl(QStringLiteral("qrc:/agentpanel/web/index.html")));
    outer->addWidget(m_transcript, 1);

    m_pendingRow = new QWidget(this);
    auto pendingRow = new QHBoxLayout(m_pendingRow);
    pendingRow->setContentsMargins(0, 0, 0, 0);
    auto pendingLabel = new QLabel(i18n("Pending"), this);
    pendingLabel->setToolTip(i18n("Editor snapshots are sent only when you press Send. Double-click to edit."));
    pendingRow->addWidget(pendingLabel);
    m_pending = new QListWidget(this);
    m_pending->setAccessibleName(i18n("Pending editor context"));
    m_pending->setSelectionMode(QAbstractItemView::ExtendedSelection);
    pendingRow->addWidget(m_pending, 1);
    auto remove = toolButton(QStringLiteral("edit-delete"), i18n("Remove Selected Context"));
    remove->setEnabled(false);
    pendingRow->addWidget(remove);
    outer->addWidget(m_pendingRow);
    m_pendingRow->hide();
    connect(m_pending->model(), &QAbstractItemModel::rowsInserted, this, &AgentPanel::updatePending);
    connect(m_pending->model(), &QAbstractItemModel::rowsRemoved, this, &AgentPanel::updatePending);
    connect(m_pending->model(), &QAbstractItemModel::modelReset, this, &AgentPanel::updatePending);
    connect(m_pending, &QListWidget::itemSelectionChanged, this, [this, remove] {
        remove->setEnabled(!m_pending->selectedItems().isEmpty());
    });
    m_input = new QPlainTextEdit(this);
    setFocusProxy(m_input);
    m_input->setAccessibleName(i18n("Message to agent"));
    m_input->setPlaceholderText(i18n("Ask the agent…   Enter to send, Ctrl+Enter for a new line"));
    m_input->setObjectName(QStringLiteral("agentComposer"));
    m_input->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_input->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_input->installEventFilter(this);
    m_input->viewport()->installEventFilter(this);
    connect(m_input->document(), &QTextDocument::contentsChanged, this, &AgentPanel::scheduleComposerLayout);
    auto composer = new QHBoxLayout;
    composer->setContentsMargins(0, 0, 0, 0);
    composer->setSpacing(0);
    m_prompt = new QLabel(QStringLiteral(">"), this);
    m_prompt->setObjectName(QStringLiteral("agentPrompt"));
    m_prompt->setForegroundRole(QPalette::PlaceholderText);
    m_prompt->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    m_prompt->setBuddy(m_input);
    composer->addWidget(m_prompt, 0, Qt::AlignTop);
    composer->addWidget(m_input, 1);
    composer->addSpacing(4);
    m_send = toolButton(QStringLiteral("go-up"), i18n("Send"));
    m_send->setObjectName(QStringLiteral("agentSubmit"));
    m_send->setToolButtonStyle(Qt::ToolButtonIconOnly);
    m_send->setIcon(QIcon::fromTheme(QStringLiteral("go-up"), style()->standardIcon(QStyle::SP_ArrowUp)));
    m_send->setEnabled(false);
    composer->addWidget(m_send, 0, Qt::AlignBottom);
    outer->addLayout(composer);
    scheduleComposerLayout();
    setStatus(i18n("Discovering agents…"));

    connect(browse, &QToolButton::clicked, this, &AgentPanel::chooseWorkspace);
    connect(refresh, &QToolButton::clicked, this, [this] {
        if (m_busy) return;
        if (m_process->state() == QProcess::NotRunning) startBridge();
        else command({{QStringLiteral("type"), QStringLiteral("discover")}});
    });
    connect(remove, &QToolButton::clicked, this, [this] { qDeleteAll(m_pending->selectedItems()); });
    connect(m_pending, &QListWidget::itemDoubleClicked, this, [this](QListWidgetItem *item) {
        QDialog dialog(this);
        dialog.setWindowTitle(i18n("Edit Pending Context"));
        dialog.resize(650, 400);
        auto layout = new QVBoxLayout(&dialog);
        auto editor = new QPlainTextEdit(item->data(Qt::UserRole).toString(), &dialog);
        layout->addWidget(editor);
        auto buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
        layout->addWidget(buttons);
        connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
        connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
        if (dialog.exec() == QDialog::Accepted) {
            item->setData(Qt::UserRole, editor->toPlainText());
            item->setToolTip(editor->toPlainText().left(3000));
        }
    });
    connect(m_send, &QToolButton::clicked, this, [this] {
        if (m_busy) command({{QStringLiteral("type"), QStringLiteral("cancel")}});
        else send();
    });
    connect(m_restore, &QToolButton::clicked, this, [this] {
        if (m_input->toPlainText().isEmpty()) m_input->setPlainText(m_lastDraft);
        else m_input->appendPlainText(m_lastDraft);
        for (const auto &[title, text] : std::as_const(m_lastPending)) {
            auto item = new QListWidgetItem(title, m_pending);
            item->setData(Qt::UserRole, text);
            item->setToolTip(text.left(3000));
        }
        m_restore->hide();
    });
    connect(m_new, &QToolButton::clicked, this, [this] {
        if (m_busy) return;
        m_workspaceEdited = false;
        m_workspace->setText(inferWorkspace());
        resetSession(true);
        m_model->clear(); m_effort->clear();
        requestConfiguration();
    });
    auto applyConfiguration = [this] {
        if (m_model->text().trimmed() != m_appliedModel || m_effort->text().trimmed() != m_appliedEffort)
            requestConfiguration(true);
    };
    connect(m_model, &QLineEdit::editingFinished, this, applyConfiguration);
    connect(m_effort, &QLineEdit::editingFinished, this, applyConfiguration);
    connect(m_workspace, &QLineEdit::textEdited, this, [this] { m_workspaceEdited = true; });
    connect(m_workspace, &QLineEdit::editingFinished, this, [this] {
        if (!m_busy && workspace() != m_sessionWorkspace) {
            resetSession();
            requestSessions(true);
        }
    });
    connect(m_window, &KTextEditor::MainWindow::viewChanged, this, [this] {
        QTimer::singleShot(0, this, &AgentPanel::syncWorkspace);
    });
    watchProject(m_window->pluginView(QStringLiteral("kateprojectplugin")));
    connect(m_window, &KTextEditor::MainWindow::pluginViewCreated, this, [this](const QString &name, QObject *project) {
        if (name == QLatin1String("kateprojectplugin")) { watchProject(project); syncWorkspace(); }
    });
    connect(m_window, &KTextEditor::MainWindow::pluginViewDeleted, this, [this](const QString &name, QObject *) {
        if (name == QLatin1String("kateprojectplugin")) QTimer::singleShot(0, this, &AgentPanel::syncWorkspace);
    });
    connect(m_agents, &QComboBox::currentIndexChanged, this, [this] {
        const auto agent = m_agents->currentData().toString();
        if (agent != m_lastAgent) {
            resetSession();
            m_model->clear(); m_effort->clear();
            m_lastAgent = agent;
            requestConfiguration();
        }
    });
    connect(m_process, &QProcess::readyReadStandardOutput, this, [this] {
        m_buffer += m_process->readAllStandardOutput();
        if (m_buffer.size() > 32 * 1024 * 1024) {
            m_process->terminate();
            event({{QStringLiteral("type"), QStringLiteral("error")}, {QStringLiteral("text"), i18n("Agent bridge record exceeded the size limit")}});
            m_buffer.clear();
            return;
        }
        qsizetype end;
        while ((end = m_buffer.indexOf('\n')) >= 0) {
            const auto line = m_buffer.left(end);
            m_buffer.remove(0, end + 1);
            const auto object = QJsonDocument::fromJson(line).object();
            if (!object.isEmpty()) event(object);
        }
    });
    connect(m_process, &QProcess::readyReadStandardError, this, [this] {
        event({{QStringLiteral("type"), QStringLiteral("message")}, {QStringLiteral("id"), QStringLiteral("bridge-stderr")},
               {QStringLiteral("role"), QStringLiteral("diagnostic")}, {QStringLiteral("append"), true},
               {QStringLiteral("text"), QString::fromUtf8(m_process->readAllStandardError())}});
    });
    connect(m_process, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
        event({{QStringLiteral("type"), QStringLiteral("error")}, {QStringLiteral("text"), m_process->errorString()}});
    });
    connect(m_process, &QProcess::finished, this, [this](int, QProcess::ExitStatus) {
        event({{QStringLiteral("type"), QStringLiteral("status")}, {QStringLiteral("busy"), false}, {QStringLiteral("text"), i18n("Bridge stopped — Refresh to reconnect")}});
        m_send->setEnabled(false);
    });
    startBridge();
}

AgentPanel::~AgentPanel()
{
    // QListWidget resets its model from its destructor, after our derived class is gone.
    disconnect(m_pending->model(), nullptr, this, nullptr);
    disconnect(m_pending, nullptr, this, nullptr);
    m_ready = false;
    // EOF lets the bridge terminate its own agent process group before exiting.
    m_process->closeWriteChannel();
    if (!m_process->waitForFinished(3500)) {
        m_process->terminate();
        if (!m_process->waitForFinished(1000)) { m_process->kill(); m_process->waitForFinished(1000); }
    }
    // Destroy the page before its profile (which is another direct child of this widget).
    delete m_transcript;
    m_transcript = nullptr;
}

void AgentPanel::startBridge()
{
    if (m_process->state() != QProcess::NotRunning) return;
    if (!m_bridgeDirectory.isValid()) {
        setStatus(i18n("Could not create the agent bridge directory"), true);
        return;
    }
    for (const auto name : {QStringLiteral("agent_bridge.py"), QStringLiteral("session_store.py"), QStringLiteral("dsh_questions.mjs")}) {
        QFile source(QStringLiteral(":/agentpanel/bridge/") + name);
        QFile target(m_bridgeDirectory.filePath(name));
        if (!source.open(QIODevice::ReadOnly) || !target.open(QIODevice::WriteOnly)) {
            setStatus(i18n("Could not prepare the agent bridge"), true);
            return;
        }
        target.write(source.readAll());
    }
    const auto python = QStandardPaths::findExecutable(QStringLiteral("python3"));
    if (python.isEmpty()) { setStatus(i18n("python3 is required"), true); return; }
    m_buffer.clear();
    m_process->start(python, {QStringLiteral("-u"), m_bridgeDirectory.filePath(QStringLiteral("agent_bridge.py"))});
}

QString AgentPanel::inferWorkspace() const
{
    if (auto project = m_window->pluginView(QStringLiteral("kateprojectplugin"))) {
        const auto path = project->property("projectBaseDir").toString();
        if (!path.isEmpty() && QDir::isAbsolutePath(path) && QFileInfo(path).isDir()) return QDir(path).absolutePath();
    }
    auto view = m_window->activeView();
    if (view && view->document()->url().isLocalFile()) {
        return QFileInfo(view->document()->url().toLocalFile()).absolutePath();
    }
    return QDir::homePath();
}

QString AgentPanel::workspace() const
{
    auto path = m_workspace->text().trimmed();
    if (path == QLatin1String("~")) path = QDir::homePath();
    else if (path.startsWith(QLatin1String("~/"))) path = QDir::homePath() + path.mid(1);
    return path.isEmpty() ? inferWorkspace() : QDir(path).absolutePath();
}

void AgentPanel::watchProject(QObject *project)
{
    if (project && project->metaObject()->indexOfSignal("projectFileNameChanged()") >= 0)
        connect(project, SIGNAL(projectFileNameChanged()), this, SLOT(syncWorkspace()), Qt::UniqueConnection);
}

void AgentPanel::syncWorkspace()
{
    if (!m_busy && !m_hasConversation && !m_workspaceEdited && !m_pending->count() && workspace() != inferWorkspace()) {
        m_workspace->setText(inferWorkspace());
        resetSession();
        if (m_discovered) requestSessions(true);
    }
}

void AgentPanel::setStatus(const QString &text, bool error)
{
    const auto action = m_busy ? i18n("Stop") : i18n("Send (Enter)");
    const bool idle = text.isEmpty() || text == i18n("Ready") || text == i18n("Stopped");
    m_send->setToolTip(idle ? action : QStringLiteral("<b>%1</b><br/>%2").arg(action.toHtmlEscaped(), text.toHtmlEscaped()));
    m_send->setAccessibleDescription(text);
    if (error) event({{QStringLiteral("type"), QStringLiteral("error")}, {QStringLiteral("text"), text}});
}

void AgentPanel::chooseWorkspace()
{
    if (m_busy) return;
    const auto folder = QFileDialog::getExistingDirectory(this, i18n("Agent Working Directory"), workspace());
    if (!folder.isEmpty()) {
        m_workspaceEdited = true;
        if (m_hasConversation) resetSession();
        m_workspace->setText(folder);
        requestSessions(true);
    }
}

void AgentPanel::command(const QJsonObject &object)
{
    if (m_process->state() != QProcess::Running) {
        setStatus(i18n("Bridge is unavailable — press Refresh"), true);
        return;
    }
    m_process->write(QJsonDocument(object).toJson(QJsonDocument::Compact) + '\n');
}

void AgentPanel::send()
{
    if (m_busy || m_loadingSession || !m_send->isEnabled() || m_agents->currentData().toString().isEmpty()) return;
    if (m_loadingConfiguration) { m_sendWhenConfigured = true; return; }
    QString text = m_input->toPlainText();
    const QString displayText = text;
    QJsonArray attachments;
    if (text.trimmed().isEmpty() && m_pending->count() == 0) return;
    if (!QFileInfo(workspace()).isDir()) { setStatus(i18n("Choose an existing working directory"), true); return; }
    if (m_hasConversation && m_sessionWorkspace != workspace()) resetSession();
    m_lastDraft = displayText;
    m_lastPending.clear();
    m_restore->hide();
    if (m_pending->count()) {
        text += QStringLiteral("\n\nEditor context snapshots (may contain unsaved changes):\n");
        for (int i = 0; i < m_pending->count(); ++i) {
            const auto item = m_pending->item(i);
            const auto payload = item->data(Qt::UserRole).toString();
            m_lastPending.append({item->text(), payload});
            text += payload + QLatin1Char('\n');
            attachments.append(QJsonObject{{QStringLiteral("title"), item->text()}, {QStringLiteral("text"), payload}});
        }
    }
    m_sessionWorkspace = workspace();
    m_hasConversation = true;
    event({{QStringLiteral("type"), QStringLiteral("message")}, {QStringLiteral("id"), QUuid::createUuid().toString()},
           {QStringLiteral("role"), QStringLiteral("user")}, {QStringLiteral("text"), displayText}, {QStringLiteral("attachments"), attachments}});
    // Disable immediately, before the asynchronous bridge acknowledges the turn.
    event({{QStringLiteral("type"), QStringLiteral("status")}, {QStringLiteral("busy"), true}, {QStringLiteral("text"), i18n("Starting…")}});
    command({{QStringLiteral("type"), QStringLiteral("send")}, {QStringLiteral("agent"), m_agents->currentData().toString()},
             {QStringLiteral("cwd"), workspace()}, {QStringLiteral("text"), text}, {QStringLiteral("displayText"), displayText},
             {QStringLiteral("attachments"), attachments}, {QStringLiteral("model"), m_model->text().trimmed()},
             {QStringLiteral("effort"), m_effort->text().trimmed()}});
    m_input->clear();
    m_pending->clear();
}

void AgentPanel::addContext(KTextEditor::View *view, bool selection)
{
    if (!view) return;
    if (selection && !view->selection()) { setStatus(i18n("Select text to add it to Pending"), true); return; }
    if (!m_hasConversation && !m_workspaceEdited && !m_pending->count()) m_workspace->setText(inferWorkspace());
    auto document = view->document();
    const auto cursor = view->cursorPosition();
    const auto range = selection ? view->selectionRange() : KTextEditor::Range(cursor.line(), 0, cursor.line(), document->lineLength(cursor.line()));
    const auto url = document->url();
    const int lastLine = selection && range.end().column() == 0 && range.end().line() > range.start().line() ? range.end().line() - 1 : range.end().line();
    const auto file = url.isLocalFile() ? workspaceReference(url.toLocalFile(), workspace(), range.start().line(), lastLine)
        : (url.isEmpty() ? document->documentName() : url.toString());
    const auto body = selection ? view->selectionText() : document->line(cursor.line());
    const auto title = file + (selection ? i18n(" (selection)") : i18n(" (cursor)"));
    // JSON content preserves exact whitespace and cannot be escaped by code fences in the selection.
    const QJsonObject snapshot{{QStringLiteral("file"), file}, {QStringLiteral("kind"), selection ? QStringLiteral("selection") : QStringLiteral("cursor")},
        {QStringLiteral("line"), selection ? range.start().line() + 1 : cursor.line() + 1},
        {QStringLiteral("column"), selection ? range.start().column() + 1 : cursor.column() + 1},
        {QStringLiteral("endLine"), range.end().line() + 1}, {QStringLiteral("endColumn"), range.end().column() + 1},
        {QStringLiteral("unsaved"), document->isModified()}, {QStringLiteral("text"), body}};
    const QString payload = QString::fromUtf8(QJsonDocument(snapshot).toJson(QJsonDocument::Indented));
    auto item = new QListWidgetItem(title, m_pending);
    item->setData(Qt::UserRole, payload);
    item->setToolTip(payload.left(3000));
    m_input->setFocus();
}

void AgentPanel::copyReference(KTextEditor::View *view)
{
    if (!view || !view->document()->url().isLocalFile()) return;
    if (!m_hasConversation && !m_workspaceEdited && !m_pending->count()) m_workspace->setText(inferWorkspace());
    const auto range = view->selection() ? view->selectionRange() : KTextEditor::Range(view->cursorPosition(), view->cursorPosition());
    const int last = range.end().column() == 0 && range.end().line() > range.start().line() ? range.end().line() - 1 : range.end().line();
    QApplication::clipboard()->setText(workspaceReference(view->document()->url().toLocalFile(), workspace(), range.start().line(), last));
}

void AgentPanel::resetSession(bool forgetLast)
{
    if (m_busy) return;
    m_sessionsRequest.clear();
    m_configurationRequest.clear();
    m_loadingConfiguration = false;
    m_sendWhenConfigured = false;
    m_loadingSession = false;
    command({{QStringLiteral("type"), QStringLiteral("reset")}, {QStringLiteral("forgetLast"), forgetLast}, {QStringLiteral("cwd"), workspace()}});
    m_history.clear();
    m_hasConversation = false;
    m_selectedSession.clear();
    m_sessionWorkspace = workspace();
    m_restore->hide();
    m_lastDraft.clear();
    m_lastPending.clear();
    if (m_ready) Q_EMIT transcriptEvent(QStringLiteral("{\"type\":\"clear\"}"));
}

bool AgentPanel::eventFilter(QObject *object, QEvent *event)
{
    if (object == m_input->viewport() && event->type() == QEvent::Resize) scheduleComposerLayout();
    if (object == m_input) {
        if (event->type() == QEvent::InputMethod) m_composing = !static_cast<QInputMethodEvent *>(event)->preeditString().isEmpty();
        if (event->type() == QEvent::Resize || event->type() == QEvent::FontChange || event->type() == QEvent::StyleChange)
            scheduleComposerLayout();
        if (event->type() == QEvent::KeyPress && !m_composing) {
            auto key = static_cast<QKeyEvent *>(event);
            if (key->key() == Qt::Key_Return || key->key() == Qt::Key_Enter) {
                const auto modifiers = key->modifiers() & ~Qt::KeypadModifier;
                if (modifiers == Qt::ControlModifier) {
                    m_input->insertPlainText(QStringLiteral("\n"));
                    return true;
                }
                if (modifiers == Qt::NoModifier) {
                    if (!key->isAutoRepeat()) send();
                    return true;
                }
            }
        }
    }
    return QWidget::eventFilter(object, event);
}

void AgentPanel::scheduleComposerLayout()
{
    if (m_composerLayoutPending) return;
    m_composerLayoutPending = true;
    QTimer::singleShot(0, this, [this] {
        m_composerLayoutPending = false;
        updateComposerLayout();
    });
}

void AgentPanel::updateComposerLayout()
{
    // Never resize from updateRequest or while Qt is editing/laying out the document.
    // Measure at most two lines independently of QPlainTextEdit's lazy block layout.
    int lines = 0;
    qreal textHeight = 0;
    const qreal margin = m_input->document()->documentMargin();
    const qreal width = qMax(1.0, m_input->viewport()->width() - 2 * margin);
    for (auto block = m_input->document()->begin(); block.isValid() && lines < 2; block = block.next()) {
        QTextLayout measurement(block.text(), m_input->font());
        measurement.setTextOption(m_input->document()->defaultTextOption());
        measurement.beginLayout();
        while (lines < 2) {
            auto line = measurement.createLine();
            if (!line.isValid()) break;
            line.setLeadingIncluded(true);
            line.setLineWidth(width);
            textHeight += line.height();
            ++lines;
        }
        measurement.endLayout();
    }
    const QFontMetrics metrics(m_input->font());
    textHeight = qMax(textHeight, qreal(qMax(metrics.height(), metrics.lineSpacing()) * qBound(1, lines, 2)));
    const int height = qCeil(textHeight + 2 * margin) + m_input->height() - m_input->viewport()->height() + 2;
    if (m_input->height() != height) m_input->setFixedHeight(height);
    if (m_prompt) {
        // Match the transcript's padding + role column + gap, including the native text inset.
        const int inset = m_input->viewport()->x() + qCeil(margin);
        m_prompt->setFixedWidth(16 + qRound(QFontInfo(font()).pixelSize() * 3.5) - inset);
        m_prompt->setContentsMargins(8, m_input->viewport()->y() + qCeil(margin), 0, 0);
    }
    const int iconSize = style()->pixelMetric(QStyle::PM_SmallIconSize);
    m_send->setIconSize(QSize(iconSize, iconSize));
    m_send->setFixedSize(QSize(iconSize + 8, iconSize + 8));
    m_input->ensureCursorVisible();
}

void AgentPanel::requestSessions(bool autoResume, bool refresh)
{
    if (m_process->state() != QProcess::Running || !QFileInfo(workspace()).isDir()) return;
    m_sessionsRequest = QUuid::createUuid().toString(QUuid::WithoutBraces);
    if (autoResume) {
        m_loadingSession = true;
        m_send->setEnabled(false);
        m_model->setEnabled(false); m_effort->setEnabled(false); m_agents->setEnabled(false);
        setStatus(i18n("Loading local sessions…"));
    }
    command({{QStringLiteral("type"), QStringLiteral("sessions")}, {QStringLiteral("cwd"), workspace()},
        {QStringLiteral("requestId"), m_sessionsRequest}, {QStringLiteral("autoResume"), autoResume}, {QStringLiteral("refresh"), refresh}});
}

void AgentPanel::requestConfiguration(bool apply)
{
    if (m_busy || m_loadingSession || m_loadingConfiguration || m_agents->currentIndex() < 0
        || m_process->state() != QProcess::Running || !QFileInfo(workspace()).isDir()) return;
    m_configurationRequest = QUuid::createUuid().toString(QUuid::WithoutBraces);
    m_loadingConfiguration = true;
    m_configurationFailed = false;
    m_model->setEnabled(false); m_effort->setEnabled(false);
    setStatus(apply ? i18n("Applying session configuration…") : i18n("Reading session configuration…"));
    command({{QStringLiteral("type"), apply ? QStringLiteral("configure") : QStringLiteral("prepare")},
        {QStringLiteral("requestId"), m_configurationRequest}, {QStringLiteral("agent"), m_agents->currentData().toString()},
        {QStringLiteral("cwd"), workspace()}, {QStringLiteral("model"), m_model->text().trimmed()},
        {QStringLiteral("effort"), m_effort->text().trimmed()}});
}

void AgentPanel::updateSessionsView()
{
    if (!m_sessionsView) return;
    const auto selected = m_sessionsView->currentItem()
        ? m_sessionsView->currentItem()->data(0, Qt::UserRole).toString() : m_selectedSession;
    m_sessionsView->clear();
    for (const auto &session : m_localSessions) {
        const auto agent = session.value(QStringLiteral("agent")).toString();
        const int agentIndex = m_agents->findData(agent);
        auto item = new QTreeWidgetItem(m_sessionsView, {
            agentIndex >= 0 ? m_agents->itemText(agentIndex) : agent,
            session.value(QStringLiteral("title")).toString(),
            QDateTime::fromSecsSinceEpoch(qint64(session.value(QStringLiteral("updated")).toDouble())).toLocalTime().toString(QStringLiteral("yyyy-MM-dd HH:mm"))});
        item->setData(0, Qt::UserRole, session.value(QStringLiteral("id")).toString());
        item->setToolTip(1, session.value(QStringLiteral("session")).toString());
        if (agentIndex < 0 || !(m_agents->model()->flags(m_agents->model()->index(agentIndex, 0)) & Qt::ItemIsEnabled))
            item->setDisabled(true);
        if (session.value(QStringLiteral("id")).toString() == selected) m_sessionsView->setCurrentItem(item);
    }
}

void AgentPanel::showSessions()
{
    if (m_busy) return;
    QDialog dialog(this);
    dialog.setWindowTitle(i18n("Local Sessions"));
    dialog.resize(720, 380);
    auto layout = new QVBoxLayout(&dialog);
    auto directory = new QLabel(workspace(), &dialog);
    directory->setTextInteractionFlags(Qt::TextSelectableByMouse);
    layout->addWidget(directory);
    auto view = new QTreeWidget(&dialog);
    view->setObjectName(QStringLiteral("agentSessionList"));
    view->setHeaderLabels({i18n("CLI Tool"), i18n("Session"), i18n("Updated")});
    view->setRootIsDecorated(false);
    view->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    view->header()->setSectionResizeMode(1, QHeaderView::Stretch);
    view->header()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    layout->addWidget(view);
    auto buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
    auto resume = buttons->addButton(i18n("Resume"), QDialogButtonBox::AcceptRole);
    auto refresh = buttons->addButton(i18n("Refresh"), QDialogButtonBox::ActionRole);
    resume->setEnabled(false);
    layout->addWidget(buttons);
    m_sessionsView = view;
    connect(view, &QTreeWidget::itemSelectionChanged, &dialog, [view, resume] {
        resume->setEnabled(view->currentItem() && !view->currentItem()->isDisabled());
    });
    updateSessionsView();
    auto select = [this, view, &dialog] {
        auto item = view->currentItem();
        if (!item || item->isDisabled()) return;
        m_loadingSession = true; m_send->setEnabled(false);
        m_model->setEnabled(false); m_effort->setEnabled(false); m_agents->setEnabled(false);
        command({{QStringLiteral("type"), QStringLiteral("resume")}, {QStringLiteral("cwd"), workspace()},
            {QStringLiteral("id"), item->data(0, Qt::UserRole).toString()}, {QStringLiteral("requestId"), m_sessionsRequest}});
        dialog.accept();
    };
    connect(resume, &QPushButton::clicked, &dialog, select);
    connect(view, &QTreeWidget::itemDoubleClicked, &dialog, select);
    connect(refresh, &QPushButton::clicked, this, [this] { requestSessions(false, true); });
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    requestSessions();
    dialog.exec();
}

void AgentPanel::event(const QJsonObject &object)
{
    const auto type = object.value(QStringLiteral("type")).toString();
    if (type == QLatin1String("inventory")) {
        const auto previous = m_agents->currentData().toString();
        const QSignalBlocker blocker(m_agents);
        m_agents->clear();
        int first = -1;
        for (const auto value : object.value(QStringLiteral("agents")).toArray()) {
            const auto agent = value.toObject();
            bool available = agent.value(QStringLiteral("available")).toBool();
            const auto label = agent.value(QStringLiteral("name")).toString() + (available ? QString() : i18n(" — unavailable"));
            m_agents->addItem(label, agent.value(QStringLiteral("id")).toString());
            const int index = m_agents->count() - 1;
            m_agents->setItemData(index, agent.value(QStringLiteral("executable")).toString() + QLatin1Char('\n') + agent.value(QStringLiteral("error")).toString(), Qt::ToolTipRole);
            if (auto model = qobject_cast<QStandardItemModel *>(m_agents->model())) model->item(index)->setEnabled(available);
            if (available && first < 0) first = index;
        }
        int index = m_agents->findData(previous);
        if (index < 0 || !(m_agents->model()->flags(m_agents->model()->index(index, 0)) & Qt::ItemIsEnabled)) index = first;
        m_agents->setCurrentIndex(index);
        m_lastAgent = m_agents->currentData().toString();
        m_send->setEnabled(first >= 0 && !m_busy);
        QStringList keys;
        for (const auto key : object.value(QStringLiteral("environment")).toArray()) keys << key.toString();
        m_agents->setToolTip(keys.isEmpty() ? i18n("Agents use their existing login or configuration.")
            : i18n("Detected environment variables:\n%1", keys.join(QLatin1Char('\n'))));
        if (!m_busy) setStatus(first >= 0 ? i18n("Ready") : i18n("No supported agent found"));
        if (!m_discovered) {
            m_discovered = true;
            requestSessions(true);
        }
        return;
    }
    if (type == QLatin1String("sessions")) {
        if (object.value(QStringLiteral("requestId")).toString() != m_sessionsRequest) return;
        m_localSessions.clear();
        for (const auto item : object.value(QStringLiteral("sessions")).toArray()) m_localSessions.append(item.toObject());
        m_selectedSession = object.value(QStringLiteral("current")).toString();
        updateSessionsView();
        return;
    }
    if (type == QLatin1String("sessions_complete")) {
        if (object.value(QStringLiteral("requestId")).toString() != m_sessionsRequest) return;
        const bool prepare = m_loadingSession;
        m_loadingSession = false;
        event({{QStringLiteral("type"), QStringLiteral("status")}, {QStringLiteral("busy"), m_busy}, {QStringLiteral("text"), i18n("Ready")}});
        if (prepare) requestConfiguration();
        return;
    }
    if (type == QLatin1String("resumed")) {
        if (object.value(QStringLiteral("requestId")).toString() != m_sessionsRequest) return;
        const QSignalBlocker blocker(m_agents);
        m_agents->setCurrentIndex(m_agents->findData(object.value(QStringLiteral("agent")).toString()));
        m_lastAgent = m_agents->currentData().toString();
        m_model->setText(object.value(QStringLiteral("model")).toString());
        m_effort->setText(object.value(QStringLiteral("effort")).toString());
        m_selectedSession = object.value(QStringLiteral("id")).toString();
        m_sessionWorkspace = workspace(); m_hasConversation = true;
        m_history.clear();
        if (m_ready) Q_EMIT transcriptEvent(QStringLiteral("{\"type\":\"clear\"}"));
        for (const auto item : object.value(QStringLiteral("history")).toArray()) event(item.toObject());
        setStatus(i18n("Resumed: %1", object.value(QStringLiteral("title")).toString()));
        m_loadingSession = false;
        requestConfiguration();
        return;
    }
    if (type == QLatin1String("configuration")) {
        if (object.value(QStringLiteral("requestId")).toString() != m_configurationRequest
            || object.value(QStringLiteral("agent")).toString() != m_agents->currentData().toString()) return;
        m_appliedModel = object.value(QStringLiteral("model")).toString();
        m_appliedEffort = object.value(QStringLiteral("effort")).toString();
        if (!m_model->isModified() || m_loadingConfiguration) m_model->setText(m_appliedModel);
        if (!m_effort->isModified() || m_loadingConfiguration) m_effort->setText(m_appliedEffort);
        return;
    }
    if (type == QLatin1String("configuration_complete")) {
        if (object.value(QStringLiteral("requestId")).toString() != m_configurationRequest) return;
        m_loadingConfiguration = false;
        event({{QStringLiteral("type"), QStringLiteral("status")}, {QStringLiteral("busy"), m_busy},
            {QStringLiteral("text"), m_configurationFailed ? i18n("Configuration failed") : i18n("Ready")}});
        if (m_sendWhenConfigured && !m_configurationFailed) { m_sendWhenConfigured = false; send(); }
        else m_sendWhenConfigured = false;
        return;
    }
    if (type == QLatin1String("session")) {
        return;
    }
    if (type == QLatin1String("status")) {
        m_busy = object.value(QStringLiteral("busy")).toBool();
        setStatus(object.value(QStringLiteral("text")).toString());
        m_send->setEnabled((m_busy || !m_loadingSession) && m_agents->currentIndex() >= 0 && m_process->state() == QProcess::Running);
        m_send->setIcon(m_busy ? QIcon::fromTheme(QStringLiteral("media-playback-stop"), style()->standardIcon(QStyle::SP_MediaStop))
            : QIcon::fromTheme(QStringLiteral("go-up"), style()->standardIcon(QStyle::SP_ArrowUp)));
        m_send->setText(m_busy ? i18n("Stop") : i18n("Send"));
        m_send->setAccessibleName(m_send->text());
        m_agents->setEnabled(!m_busy && !m_loadingSession);
        m_workspace->setEnabled(!m_busy);
        m_new->setEnabled(!m_busy);
        m_sessions->setEnabled(!m_busy);
        m_model->setEnabled(!m_busy && !m_loadingSession && !m_loadingConfiguration);
        m_effort->setEnabled(!m_busy && !m_loadingSession && !m_loadingConfiguration);
        if (m_ready) Q_EMIT transcriptEvent(QString::fromUtf8(QJsonDocument(object).toJson(QJsonDocument::Compact)));
        return;
    }
    if (type == QLatin1String("error") && (object.value(QStringLiteral("operation")) == QLatin1String("prepare")
        || object.value(QStringLiteral("operation")) == QLatin1String("configure"))) {
        if (object.value(QStringLiteral("requestId")).toString() != m_configurationRequest) return;
        m_configurationFailed = true;
    }
    if (type == QLatin1String("error") && (object.value(QStringLiteral("operation")) == QLatin1String("sessions") || object.value(QStringLiteral("operation")) == QLatin1String("resume"))) {
        m_loadingSession = false;
        event({{QStringLiteral("type"), QStringLiteral("status")}, {QStringLiteral("busy"), m_busy}, {QStringLiteral("text"), i18n("Ready")}});
    }
    if (type == QLatin1String("error") && (!m_lastDraft.isEmpty() || !m_lastPending.isEmpty())) m_restore->show();
    if (type == QLatin1String("message") || type == QLatin1String("tool")) {
        const auto id = object.value(QStringLiteral("id"));
        bool merged = false;
        for (auto &entry : m_history) {
            if (entry.value(QStringLiteral("type")) == type && entry.value(QStringLiteral("id")) == id) {
                auto next = object;
                if (object.value(QStringLiteral("append")).toBool()) next[QStringLiteral("text")] =
                    (entry.value(QStringLiteral("text")).toString() + object.value(QStringLiteral("text")).toString()).right(2 * 1024 * 1024);
                next[QStringLiteral("append")] = false;
                entry = next;
                merged = true;
                break;
            }
        }
        if (!merged) m_history.append(object);
    } else {
        m_history.append(object);
    }
    while (m_history.size() > 1000) m_history.removeFirst();
    if (m_ready) Q_EMIT transcriptEvent(QString::fromUtf8(QJsonDocument(object).toJson(QJsonDocument::Compact)));
}

void AgentPanel::rendererReady()
{
    m_ready = true;
    Q_EMIT transcriptEvent(QStringLiteral("{\"type\":\"clear\"}"));
    for (const auto &object : m_history) Q_EMIT transcriptEvent(QString::fromUtf8(QJsonDocument(object).toJson(QJsonDocument::Compact)));
    if (m_busy) Q_EMIT transcriptEvent(QStringLiteral("{\"type\":\"status\",\"busy\":true}"));
    updateTheme();
}

void AgentPanel::changeEvent(QEvent *event)
{
    QWidget::changeEvent(event);
    if (event->type() == QEvent::PaletteChange || event->type() == QEvent::ApplicationPaletteChange
        || event->type() == QEvent::FontChange || event->type() == QEvent::ApplicationFontChange) {
        updateTheme();
    }
}

void AgentPanel::updatePending()
{
    m_pendingRow->setVisible(m_pending->count() > 0);
    if (m_pending->count()) {
        m_pending->setFixedHeight(qMax(1, m_pending->sizeHintForRow(0)) * qMin(3, m_pending->count()) + 2 * m_pending->frameWidth());
    }
}

void AgentPanel::updateTheme()
{
    if (!m_transcript) return;
    const auto palette = this->palette();
    m_transcript->page()->setBackgroundColor(palette.color(QPalette::Base));
    if (!m_ready) return;
    scheduleComposerLayout();
    updatePending();
    Q_EMIT transcriptEvent(QString::fromUtf8(QJsonDocument(QJsonObject{
        {QStringLiteral("type"), QStringLiteral("theme")}, {QStringLiteral("background"), palette.color(QPalette::Base).name()},
        {QStringLiteral("foreground"), palette.color(QPalette::Text).name()}, {QStringLiteral("accent"), palette.color(QPalette::Link).name()},
        {QStringLiteral("muted"), palette.color(QPalette::PlaceholderText).name()
            + QStringLiteral("%1").arg(palette.color(QPalette::PlaceholderText).alpha(), 2, 16, QLatin1Char('0'))},
        {QStringLiteral("alternate"), palette.color(QPalette::AlternateBase).name()},
        {QStringLiteral("border"), palette.color(QPalette::Mid).name()}, {QStringLiteral("selection"), palette.color(QPalette::Highlight).name()},
        {QStringLiteral("selectedText"), palette.color(QPalette::HighlightedText).name()},
        {QStringLiteral("colorScheme"), palette.color(QPalette::Base).lightness() < 128 ? QStringLiteral("dark") : QStringLiteral("light")},
        {QStringLiteral("fontFamily"), QFontInfo(font()).family()}, {QStringLiteral("fontSize"), QFontInfo(font()).pixelSize()},
        {QStringLiteral("messageGutter"), qRound(QFontInfo(font()).pixelSize() * 3.5)},
        {QStringLiteral("fixedFontFamily"), QFontInfo(QFontDatabase::systemFont(QFontDatabase::FixedFont)).family()}
    }).toJson(QJsonDocument::Compact)));
}

void AgentPanel::openLink(const QString &href)
{
    const QUrl url(href);
    if (url.scheme() == QLatin1String("https") || url.scheme() == QLatin1String("http")) {
        QDesktopServices::openUrl(url);
        return;
    }
    const auto file = resolveFileLink(href, m_sessionWorkspace.isEmpty() ? workspace() : m_sessionWorkspace);
    if (!file.valid) { setStatus(i18n("File not found: %1", href.left(150)), true); return; }
    if (auto view = m_window->openUrl(file.url)) {
        view->removeSelection();
        const int line = qMin(file.line, qMax(0, view->document()->lines() - 1));
        view->setCursorPosition(KTextEditor::Cursor(line, qMin(file.column, view->document()->lineLength(line))));
        if (file.endLine >= 0) {
            const int end = qMin(file.endLine, view->document()->lines() - 1);
            view->setSelection(KTextEditor::Range(line, 0, end, view->document()->lineLength(end)));
        }
    }
}

void AgentPanel::reply(const QString &id, const QString &jsonValue)
{
    const auto array = QJsonDocument::fromJson((QLatin1Char('[') + jsonValue + QLatin1Char(']')).toUtf8()).array();
    if (array.size() != 1) return;
    command({{QStringLiteral("type"), QStringLiteral("reply")}, {QStringLiteral("id"), id}, {QStringLiteral("value"), array.first()}});
}

TranscriptBridge::TranscriptBridge(AgentPanel *panel) : QObject(panel), m_panel(panel)
{
    connect(panel, &AgentPanel::transcriptEvent, this, &TranscriptBridge::transcriptEvent);
}
void TranscriptBridge::rendererReady() { m_panel->rendererReady(); }
void TranscriptBridge::openLink(const QString &href) { m_panel->openLink(href); }
void TranscriptBridge::reply(const QString &id, const QString &jsonValue) { m_panel->reply(id, jsonValue); }
