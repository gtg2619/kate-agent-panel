#include "panel.h"
#include <KActionCollection>
#include <KLocalizedString>
#include <KPluginFactory>
#include <KTextEditor/MainWindow>
#include <KTextEditor/Plugin>
#include <KTextEditor/View>
#include <KXMLGUIClient>
#include <KXMLGUIFactory>
#include <QAction>
#include <QApplication>
#include <QEvent>
#include <QIcon>
#include <QMenu>
#include <QShortcut>
#include <QStyle>
#include <QTimer>
#include <QVBoxLayout>

class AgentPluginView : public QObject, public KXMLGUIClient
{
    Q_OBJECT
public:
    AgentPluginView(KTextEditor::Plugin *plugin, KTextEditor::MainWindow *window)
        : QObject(window), m_window(window)
    {
        const auto agentIcon = QIcon::fromTheme(QStringLiteral("dialog-messages"),
            QApplication::style()->standardIcon(QStyle::SP_MessageBoxInformation));
        m_tool = window->createToolView(plugin, QStringLiteral("kate_agent_panel"),
            KTextEditor::MainWindow::Bottom, agentIcon, i18n("Agent"));
        if (!m_tool) return;
        m_tool->installEventFilter(this);
        if (m_tool->isVisible()) schedulePanel();
        auto escape = new QShortcut(QKeySequence(Qt::Key_Escape), m_tool);
        escape->setContext(Qt::WidgetWithChildrenShortcut);
        connect(escape, &QShortcut::activated, this, [this] {
            m_window->hideToolView(m_tool);
            if (auto view = m_window->activeView()) view->setFocus();
        });
        setComponentName(QStringLiteral("kateagentpanel"), i18n("Agent Panel"));
        setXMLFile(QStringLiteral(":/agentpanel/ui.rc"));

        auto show = actionCollection()->addAction(QStringLiteral("agent_panel_show"));
        show->setIcon(agentIcon);
        show->setText(i18n("Show Agent Panel"));
        KActionCollection::setDefaultShortcut(show, QKeySequence(QStringLiteral("Ctrl+Alt+A")));
        connect(show, &QAction::triggered, this, [this] {
            m_window->showToolView(m_tool);
            ensurePanel()->setFocus();
        });
        auto selection = actionCollection()->addAction(QStringLiteral("agent_pending_selection"));
        selection->setText(i18n("Add Selection to Agent Pending"));
        KActionCollection::setDefaultShortcut(selection, QKeySequence(QStringLiteral("Ctrl+Alt+S")));
        connect(selection, &QAction::triggered, this, [this] { stage(m_window->activeView(), true); });
        auto cursor = actionCollection()->addAction(QStringLiteral("agent_pending_cursor"));
        cursor->setText(i18n("Add Cursor Context to Agent Pending"));
        KActionCollection::setDefaultShortcut(cursor, QKeySequence(QStringLiteral("Ctrl+Alt+L")));
        connect(cursor, &QAction::triggered, this, [this] { stage(m_window->activeView(), false); });
        auto copy = actionCollection()->addAction(QStringLiteral("agent_copy_reference"));
        copy->setText(i18n("Copy Workspace Code Reference"));
        connect(copy, &QAction::triggered, this, [this] { ensurePanel()->copyReference(m_window->activeView()); });
        if (window->guiFactory()) window->guiFactory()->addClient(this);
        for (auto view : window->views()) hookView(view);
        connect(window, &KTextEditor::MainWindow::viewCreated, this, &AgentPluginView::hookView);
    }

    ~AgentPluginView() override
    {
        for (auto menu : std::as_const(m_contextMenus)) delete menu;
        if (m_window->guiFactory()) m_window->guiFactory()->removeClient(this);
        // Tool view is owned by the window; explicitly remove it when the plugin is disabled.
        delete m_tool;
    }

private:
    bool eventFilter(QObject *object, QEvent *event) override
    {
        if (object == m_tool && event->type() == QEvent::Show && !m_panel) schedulePanel();
        return QObject::eventFilter(object, event);
    }
    void schedulePanel()
    {
        // Session restoration can show tools while Kate is still constructing its
        // window. Leave that stack before initializing WebEngine and the bridge.
        QTimer::singleShot(0, this, [this] {
            if (m_tool && m_tool->isVisible()) ensurePanel();
        });
    }
    AgentPanel *ensurePanel()
    {
        if (m_panel) return m_panel;
        // Kate owns this layout and may automatically insert child widgets.
        auto layout = m_tool->layout();
        if (!layout) {
            layout = new QVBoxLayout(m_tool);
            layout->setContentsMargins(0, 0, 0, 0);
        }
        m_panel = new AgentPanel(m_window, m_tool);
        if (layout->indexOf(m_panel) < 0) layout->addWidget(m_panel);
        return m_panel;
    }
    void stage(KTextEditor::View *view, bool selection)
    {
        if (!view) return;
        ensurePanel()->addContext(view, selection);
        m_window->showToolView(m_tool);
    }
    void hookView(KTextEditor::View *view)
    {
        connect(view, &KTextEditor::View::contextMenuAboutToShow, this,
            [this](KTextEditor::View *source, QMenu *menu) {
                // Context actions capture the clicked view, including inactive split views.
                for (auto action : menu->actions()) {
                    if (action->property("kateAgentContext").toBool()) delete action->menu();
                }
                m_contextMenus.removeIf([](const auto &entry) { return entry.isNull(); });
                auto submenu = new QMenu(i18n("Agent Pending"), menu);
                m_contextMenus.append(submenu);
                auto root = menu->addMenu(submenu);
                root->setProperty("kateAgentContext", true);
                QPointer<KTextEditor::View> safeView(source);
                auto selected = submenu->addAction(i18n("Add Selection"));
                selected->setEnabled(source->selection());
                connect(selected, &QAction::triggered, this, [this, safeView] { stage(safeView, true); });
                auto cursor = submenu->addAction(i18n("Add Cursor Context"));
                connect(cursor, &QAction::triggered, this, [this, safeView] { stage(safeView, false); });
                auto copy = submenu->addAction(i18n("Copy Workspace Code Reference"));
                connect(copy, &QAction::triggered, this, [this, safeView] { if (safeView) ensurePanel()->copyReference(safeView); });
            });
    }
    KTextEditor::MainWindow *m_window;
    QPointer<QWidget> m_tool;
    QPointer<AgentPanel> m_panel;
    QList<QPointer<QMenu>> m_contextMenus;
};

class AgentPlugin : public KTextEditor::Plugin
{
    Q_OBJECT
public:
    explicit AgentPlugin(QObject *parent) : KTextEditor::Plugin(parent) {}
    QObject *createView(KTextEditor::MainWindow *window) override { return new AgentPluginView(this, window); }
};

K_PLUGIN_FACTORY_WITH_JSON(AgentPluginFactory, "plugin.json", registerPlugin<AgentPlugin>();)
#include "plugin.moc"
