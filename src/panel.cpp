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
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QEvent>
#include <QFontDatabase>
#include <QFontInfo>
#include <QFontMetrics>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QShortcut>
#include <QStandardItemModel>
#include <QStandardPaths>
#include <QTimer>
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
    m_workspace->setAccessibleName(i18n("Working directory"));
    m_workspace->setToolTip(i18n("Working directory for this conversation"));
    toolbar->addWidget(m_workspace, 1);
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
    m_new = toolButton(QStringLiteral("document-new"), i18n("New Chat"));
    toolbar->addWidget(m_new);
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
    m_input->setAccessibleName(i18n("Message to agent"));
    m_input->setPlaceholderText(i18n("Ask the agent…   Ctrl+Enter to send"));
    m_input->setFixedHeight(QFontMetrics(m_input->font()).lineSpacing() * 3 + 8);
    outer->addWidget(m_input);
    auto footer = new QHBoxLayout;
    m_status = new QLabel(i18n("Discovering agents…"), this);
    footer->addWidget(m_status);
    footer->addStretch();
    m_restore = new QPushButton(i18n("Restore last input"), this);
    m_restore->hide();
    footer->addWidget(m_restore);
    m_stop = new QPushButton(i18n("Stop"), this);
    m_stop->setEnabled(false);
    m_stop->hide();
    footer->addWidget(m_stop);
    m_send = new QPushButton(i18n("Send"), this);
    m_send->setEnabled(false);
    footer->addWidget(m_send);
    outer->addLayout(footer);

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
    connect(m_send, &QPushButton::clicked, this, &AgentPanel::send);
    connect(m_restore, &QPushButton::clicked, this, [this] {
        if (m_input->toPlainText().isEmpty()) m_input->setPlainText(m_lastDraft);
        else m_input->appendPlainText(m_lastDraft);
        for (const auto &[title, text] : std::as_const(m_lastPending)) {
            auto item = new QListWidgetItem(title, m_pending);
            item->setData(Qt::UserRole, text);
            item->setToolTip(text.left(3000));
        }
        m_restore->hide();
    });
    auto sendShortcut = new QShortcut(QKeySequence(QStringLiteral("Ctrl+Return")), this);
    sendShortcut->setContext(Qt::WidgetWithChildrenShortcut);
    connect(sendShortcut, &QShortcut::activated, this, &AgentPanel::send);
    connect(m_stop, &QPushButton::clicked, this, [this] { command({{QStringLiteral("type"), QStringLiteral("cancel")}}); });
    connect(m_new, &QToolButton::clicked, this, &AgentPanel::resetSession);
    connect(m_workspace, &QLineEdit::textEdited, this, [this] { m_workspaceEdited = true; });
    connect(m_window, &KTextEditor::MainWindow::viewChanged, this, [this] {
        if (!m_hasConversation && !m_workspaceEdited && !m_pending->count()) m_workspace->setText(inferWorkspace());
    });
    connect(m_agents, &QComboBox::currentIndexChanged, this, [this] {
        const auto agent = m_agents->currentData().toString();
        if (agent != m_lastAgent && m_hasConversation) resetSession();
        m_lastAgent = agent;
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
    if (!m_bridgeFile.isOpen()) {
        QFile source(QStringLiteral(":/agentpanel/bridge/agent_bridge.py"));
        if (!source.open(QIODevice::ReadOnly) || !m_bridgeFile.open()) {
            m_status->setText(i18n("Could not prepare the agent bridge"));
            return;
        }
        m_bridgeFile.write(source.readAll());
        m_bridgeFile.flush();
    }
    const auto python = QStandardPaths::findExecutable(QStringLiteral("python3"));
    if (python.isEmpty()) { m_status->setText(i18n("python3 is required")); return; }
    m_buffer.clear();
    m_process->start(python, {QStringLiteral("-u"), m_bridgeFile.fileName()});
}

QString AgentPanel::inferWorkspace() const
{
    auto view = m_window->activeView();
    if (view && view->document()->url().isLocalFile()) {
        QDir dir(QFileInfo(view->document()->url().toLocalFile()).absolutePath());
        const auto fallback = dir.absolutePath();
        do {
            if (dir.exists(QStringLiteral(".git")) || dir.exists(QStringLiteral(".kateproject"))) return dir.absolutePath();
        } while (dir.cdUp());
        return fallback;
    }
    return QDir::currentPath();
}

QString AgentPanel::workspace() const { return QDir(m_workspace->text()).absolutePath(); }

void AgentPanel::chooseWorkspace()
{
    if (m_busy) return;
    const auto folder = QFileDialog::getExistingDirectory(this, i18n("Agent Working Directory"), workspace());
    if (!folder.isEmpty()) {
        m_workspaceEdited = true;
        if (m_hasConversation) resetSession();
        m_workspace->setText(folder);
    }
}

void AgentPanel::command(const QJsonObject &object)
{
    if (m_process->state() != QProcess::Running) {
        m_status->setText(i18n("Bridge is unavailable — press Refresh"));
        return;
    }
    m_process->write(QJsonDocument(object).toJson(QJsonDocument::Compact) + '\n');
}

void AgentPanel::send()
{
    if (m_busy || !m_send->isEnabled() || m_agents->currentData().toString().isEmpty()) return;
    QString text = m_input->toPlainText();
    const QString displayText = text;
    QJsonArray attachments;
    if (text.trimmed().isEmpty() && m_pending->count() == 0) return;
    if (!QFileInfo(workspace()).isDir()) { m_status->setText(i18n("Choose an existing working directory")); return; }
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
             {QStringLiteral("cwd"), workspace()}, {QStringLiteral("text"), text}});
    m_input->clear();
    m_pending->clear();
}

void AgentPanel::addContext(KTextEditor::View *view, bool selection)
{
    if (!view) return;
    if (selection && !view->selection()) { m_status->setText(i18n("Select text to add it to Pending")); return; }
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

void AgentPanel::resetSession()
{
    if (m_busy) return;
    command({{QStringLiteral("type"), QStringLiteral("reset")}});
    m_history.clear();
    m_hasConversation = false;
    m_restore->hide();
    m_lastDraft.clear();
    m_lastPending.clear();
    m_sessionWorkspace.clear();
    if (m_ready) Q_EMIT transcriptEvent(QStringLiteral("{\"type\":\"clear\"}"));
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
        if (!m_busy) m_status->setText(first >= 0 ? i18n("Ready") : i18n("No supported agent found"));
        return;
    }
    if (type == QLatin1String("status")) {
        m_busy = object.value(QStringLiteral("busy")).toBool();
        m_status->setText(object.value(QStringLiteral("text")).toString());
        m_send->setEnabled(!m_busy && m_agents->currentIndex() >= 0 && m_process->state() == QProcess::Running);
        m_stop->setEnabled(m_busy);
        m_stop->setVisible(m_busy);
        m_send->setVisible(!m_busy);
        m_agents->setEnabled(!m_busy);
        m_workspace->setEnabled(!m_busy);
        m_new->setEnabled(!m_busy);
        return;
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
    m_input->setFixedHeight(QFontMetrics(m_input->font()).lineSpacing() * 3 + 8);
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
    if (!file.valid) { m_status->setText(i18n("File not found: %1", href.left(150))); return; }
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
