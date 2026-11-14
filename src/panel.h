#pragma once
#include <QJsonObject>
#include <QPointer>
#include <QTemporaryDir>
#include <QWidget>

class QComboBox;
class QLabel;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QPushButton;
class QProcess;
class QToolButton;
class QWebEngineView;
class QTreeWidget;
namespace KTextEditor { class MainWindow; class View; }

class AgentPanel : public QWidget
{
    Q_OBJECT
public:
    explicit AgentPanel(KTextEditor::MainWindow *window, QWidget *parent = nullptr);
    ~AgentPanel() override;
    void addContext(KTextEditor::View *view, bool selection);
    void copyReference(KTextEditor::View *view);

Q_SIGNALS:
    void transcriptEvent(const QString &json);

public Q_SLOTS:
    void rendererReady();
    void openLink(const QString &href);
    void reply(const QString &id, const QString &jsonValue);

protected:
    void changeEvent(QEvent *event) override;
    bool eventFilter(QObject *object, QEvent *event) override;

private:
    void send();
    void startBridge();
    void command(const QJsonObject &object);
    void event(const QJsonObject &object);
    void resetSession(bool forgetLast = false);
    void chooseWorkspace();
    void updateTheme();
    void updatePending();
    void updateInputHeight();
    void requestSessions(bool autoResume = false, bool refresh = false);
    void showSessions();
    void updateSessionsView();
    void requestConfiguration(bool apply = false);
    void watchProject(QObject *project);
    void setStatus(const QString &text, bool error = false);
    QString inferWorkspace() const;
    QString workspace() const;
    KTextEditor::MainWindow *m_window;
    QComboBox *m_agents;
    QLineEdit *m_workspace;
    QLineEdit *m_model;
    QLineEdit *m_effort;
    QToolButton *m_sessions;
    QPointer<QTreeWidget> m_sessionsView;
    QList<QJsonObject> m_localSessions;
    QString m_sessionsRequest;
    QString m_selectedSession;
    bool m_loadingSession = false;
    bool m_discovered = false;
    bool m_composing = false;
    QWebEngineView *m_transcript = nullptr;
    QWidget *m_pendingRow;
    QListWidget *m_pending;
    QPlainTextEdit *m_input;
    QToolButton *m_send;
    QToolButton *m_new;
    QToolButton *m_restore;
    QProcess *m_process;
    QTemporaryDir m_bridgeDirectory;
    QByteArray m_buffer;
    QList<QJsonObject> m_history;
    bool m_ready = false;
    bool m_busy = false;
    bool m_hasConversation = false;
    bool m_workspaceEdited = false;
    QString m_lastAgent;
    QString m_sessionWorkspace;
    QString m_lastDraft;
    QList<QPair<QString, QString>> m_lastPending;
    QString m_configurationRequest;
    QString m_appliedModel;
    QString m_appliedEffort;
    bool m_loadingConfiguration = false;
    bool m_sendWhenConfigured = false;
    bool m_configurationFailed = false;
private Q_SLOTS:
    void syncWorkspace();
};

// Expose only transcript operations to JavaScript, not QWidget's inherited slots/properties.
class TranscriptBridge : public QObject
{
    Q_OBJECT
public:
    explicit TranscriptBridge(AgentPanel *panel);
Q_SIGNALS:
    void transcriptEvent(const QString &json);
public Q_SLOTS:
    void rendererReady();
    void openLink(const QString &href);
    void reply(const QString &id, const QString &jsonValue);
private:
    AgentPanel *m_panel;
};
