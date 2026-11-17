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
#include <QMouseEvent>
#include <QKeyEvent>
#include <QSplitter>
#include <QStyle>
#include <QTimer>
#include <QVBoxLayout>
#include <QWindow>

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
        qApp->installEventFilter(this);
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
        endDockDrag();
        for (auto menu : std::as_const(m_contextMenus)) delete menu;
        if (m_window->guiFactory()) m_window->guiFactory()->removeClient(this);
        // Tool view is owned by the window; explicitly remove it when the plugin is disabled.
        delete m_tool;
    }

private:
    bool eventFilter(QObject *object, QEvent *event) override
    {
        if (object == m_tool && event->type() == QEvent::Show && !m_panel) schedulePanel();
        if (object == m_panel && event->type() == QEvent::LayoutRequest) updateDockMinimum();
        if (m_dragSurface && ((event->type() == QEvent::MouseButtonRelease
                && static_cast<QMouseEvent *>(event)->button() == Qt::LeftButton)
            || event->type() == QEvent::ApplicationDeactivate
            || (object == m_dragHandle && event->type() == QEvent::UngrabMouse)
            || (object == m_tool && event->type() == QEvent::Hide)
            || (m_tool && object == m_tool->window() && event->type() == QEvent::WindowDeactivate))) {
            endDockDrag();
        }
        if (event->type() == QEvent::MouseButtonPress && m_panel && m_tool && m_tool->isVisible()
            && QGuiApplication::platformName().startsWith(QLatin1String("wayland"))
            && static_cast<QMouseEvent *>(event)->button() == Qt::LeftButton) {
            auto handle = qobject_cast<QSplitterHandle *>(object);
            if (handle && handle->splitter()->isAncestorOf(m_tool) && m_panel->windowHandle()) {
                endDockDrag();
                // Same native RHI surface as Voice Panel: on Wayland it can take
                // pointer motion from the parent splitter when the dock shrinks.
                // Preserve painting but leave input with the splitter for this drag.
                m_dragHandle = handle;
                m_dragSurface = m_panel->windowHandle();
                m_dragWasTransparent = m_dragSurface->flags().testFlag(Qt::WindowTransparentForInput);
                m_dragSurface->setFlag(Qt::WindowTransparentForInput, true);
            }
        }
        if (event->type() == QEvent::ShortcutOverride || event->type() == QEvent::KeyPress) {
            const auto key = static_cast<QKeyEvent *>(event);
            const auto target = qobject_cast<QWidget *>(object);
            const auto focus = target ? target : QApplication::focusWidget();
            // Native tool surfaces must not steal Escape from modal dialogs or menus.
            if (key->key() == Qt::Key_Escape && key->modifiers() == Qt::NoModifier
                && m_tool && m_tool->isVisible() && QApplication::activeWindow() == m_tool->window()
                && focus && focus->window() == m_tool->window()
                && (focus == m_tool || m_tool->isAncestorOf(focus))
                && !QApplication::activeModalWidget() && !QApplication::activePopupWidget()) {
                event->accept();
                if (event->type() == QEvent::KeyPress) {
                    m_window->hideToolView(m_tool);
                    if (auto view = m_window->activeView()) view->setFocus();
                }
                return true;
            }
        }
        return QObject::eventFilter(object, event);
    }
    void endDockDrag()
    {
        if (m_dragSurface) m_dragSurface->setFlag(Qt::WindowTransparentForInput, m_dragWasTransparent);
        m_dragSurface.clear();
        m_dragHandle.clear();
    }
    void updateDockMinimum()
    {
        if (!m_tool || !m_panel) return;
        // Constrain only the real controls, never ToolView::sizeHint(), which
        // reflects the current (possibly enlarged/restored) dock dimensions.
        m_tool->setMinimumHeight(qMax(1, m_panel->layout()->minimumSize().height()));
    }
    void schedulePanel()
    {
        const auto host = m_tool->window()->windowHandle();
        const bool focusComposer = host && host->isExposed();
        // Session restoration can show tools while Kate is still constructing its
        // window. Leave that stack before initializing WebEngine and the bridge.
        QTimer::singleShot(0, this, [this, focusComposer] {
            if (!m_tool || !m_tool->isVisible()) return;
            auto panel = ensurePanel();
            // A click in an existing window should focus the newly created input.
            // Restoring a tool during startup must leave the editor's focus alone.
            if (focusComposer) panel->setFocus();
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
        layout->setSizeConstraint(QLayout::SetNoConstraint);
        m_panel = new AgentPanel(m_window, m_tool);
        m_panel->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Ignored);
        if (layout->indexOf(m_panel) < 0) layout->addWidget(m_panel);
        m_panel->show();
        updateDockMinimum();
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
    QPointer<QWindow> m_dragSurface;
    QPointer<QSplitterHandle> m_dragHandle;
    bool m_dragWasTransparent = false;
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
