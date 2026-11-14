#include "panel.h"
#include <KTextEditor/Document>
#include <KTextEditor/Editor>
#include <KTextEditor/MainWindow>
#include <KTextEditor/View>
#include <KTextEditor/Plugin>
#include <KPluginFactory>
#include <KActionCollection>
#include <KXMLGUIClient>
#include <KXMLGUIFactory>
#include <QApplication>
#include <QChildEvent>
#include <QClipboard>
#include <QComboBox>
#include <QDialog>
#include <QDir>
#include <QEventLoop>
#include <QElapsedTimer>
#include <QFile>
#include <QFrame>
#include <QInputMethodEvent>
#include <QJsonDocument>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QPlainTextEdit>
#include <QPluginLoader>
#include <QPushButton>
#include <QSignalSpy>
#include <QScrollBar>
#include <QScopeGuard>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>
#include <QToolButton>
#include <QTreeWidget>
#include <QUuid>
#include <QVBoxLayout>
#include <QWebEnginePage>
#include <QWebEngineView>
#include <memory>

// Kate owns the tool-view layout and automatically inserts child widgets.
class KateToolViewHost : public QFrame
{
public:
    KateToolViewHost()
    {
        auto box = new QVBoxLayout(this);
        box->setContentsMargins(0, 0, 0, 0);
        box->setSpacing(0);
    }
protected:
    void childEvent(QChildEvent *event) override
    {
        if (event->added()) {
            if (auto widget = qobject_cast<QWidget *>(event->child())) {
                layout()->addWidget(widget);
                setFocusProxy(widget);
            }
        }
        QFrame::childEvent(event);
    }
};

class Host : public QObject
{
    Q_OBJECT
public:
    KTextEditor::View *view = nullptr;
    QUrl opened;
    QPointer<QWidget> tool;
    QPointer<QWidget> shown;
    QWidget *toolParent = nullptr;
    QObject *project = nullptr;
    int hideCount = 0;
    bool managedLayout = true;
    QPointer<QLayout> toolLayout;
    QIcon toolIcon;
    KTextEditor::MainWindow::ToolViewPosition toolPosition = KTextEditor::MainWindow::Left;
public Q_SLOTS:
    KTextEditor::View *activeView() { return view; }
    QObject *pluginView(const QString &name) { return name == QLatin1String("kateprojectplugin") ? project : nullptr; }
    KTextEditor::View *openUrl(const QUrl &url, const QString &)
    {
        opened = url;
        if (view && view->document()->url() != url && !view->document()->openUrl(url)) return nullptr;
        return view;
    }
    QWidget *window() { return view ? view->window() : nullptr; }
    QWidget *createViewBar(KTextEditor::View *) { return nullptr; }
    void deleteViewBar(KTextEditor::View *) {}
    bool showToolView(QWidget *widget) { shown = widget; widget->show(); return true; }
    bool hideToolView(QWidget *widget) { ++hideCount; widget->hide(); shown = nullptr; return true; }
    KXMLGUIFactory *guiFactory() { return nullptr; }
    QList<KTextEditor::View *> views() { return view ? QList<KTextEditor::View *>{view} : QList<KTextEditor::View *>{}; }
    QWidget *createToolView(KTextEditor::Plugin *, const QString &, KTextEditor::MainWindow::ToolViewPosition position, const QIcon &icon, const QString &)
    {
        toolPosition = position;
        toolIcon = icon;
        tool = managedLayout ? new KateToolViewHost : new QWidget;
        if (toolParent) {
            tool->setParent(toolParent);
            toolParent->layout()->addWidget(tool);
            tool->hide();
        }
        toolLayout = tool->layout();
        return tool;
    }
};

class PanelTest : public QObject
{
    Q_OBJECT
    std::unique_ptr<QTemporaryDir> m_state;
    QVariant evaluate(QWebEngineView *browser, const QString &script)
    {
        QVariant result;
        QEventLoop loop;
        QPointer<QEventLoop> guard(&loop);
        QTimer::singleShot(5000, &loop, &QEventLoop::quit);
        browser->page()->runJavaScript(script, [guard, &result](const QVariant &value) {
            if (!guard) return;
            result = value;
            guard->quit();
        });
        loop.exec();
        return result;
    }

    bool waitForScript(QWebEngineView *browser, const QString &script)
    {
        QElapsedTimer timer; timer.start();
        do {
            if (evaluate(browser, script).toBool()) return true;
            QTest::qWait(25);
        } while (timer.elapsed() < 10000);
        return false;
    }

private Q_SLOTS:
    void init()
    {
        m_state = std::make_unique<QTemporaryDir>();
        qputenv("KATE_AGENT_STATE_HOME", m_state->path().toUtf8());
        qputenv("KATE_AGENT_DISABLE_NATIVE_DISCOVERY", "1");
        qputenv("KATE_AGENT_PI_COMMAND", QByteArray("python3 ") + SOURCE_DIR + "/tests/fake_agent.py");
    }

    void pluginLoadsAtBottomAndUnloads_data()
    {
        QTest::addColumn<bool>("managedLayout");
        QTest::newRow("kate-toolview") << true;
        QTest::newRow("plain-toolview") << false;
    }

    void pluginLoadsAtBottomAndUnloads()
    {
        QFETCH(bool, managedLayout);
        Host host;
        host.managedLayout = managedLayout;
        KTextEditor::MainWindow mainWindow(&host);
        QPluginLoader loader(QStringLiteral(PLUGIN_FILE));
        auto factory = qobject_cast<KPluginFactory *>(loader.instance());
        QVERIFY2(factory, qPrintable(loader.errorString()));
        auto plugin = factory->create<KTextEditor::Plugin>(&host);
        QVERIFY(plugin);
        auto pluginView = plugin->createView(&mainWindow);
        QVERIFY(pluginView); QVERIFY(host.tool);
        QCOMPARE(host.toolPosition, KTextEditor::MainWindow::Bottom);
        host.tool->resize(900, 350);
        host.tool->show();
        QVERIFY(QTest::qWaitForWindowExposed(host.tool));
        QTRY_VERIFY(host.tool->findChild<QWidget *>(QStringLiteral("kateAgentPanel")));
        auto panel = host.tool->findChild<QWidget *>(QStringLiteral("kateAgentPanel"));
        QVERIFY(panel);
        QVERIFY(host.tool->layout());
        if (managedLayout) QCOMPARE(host.tool->layout(), host.toolLayout.data());
        QVERIFY2(host.tool->layout()->indexOf(panel) >= 0, "The panel must remain in Kate's installed layout");
        for (const QSize size : {QSize(900, 350), QSize(1200, 550), QSize(700, 260)}) {
            host.tool->resize(size);
            QTRY_COMPARE(panel->geometry(), host.tool->contentsRect());
            auto input = panel->findChild<QPlainTextEdit *>();
            auto browser = panel->findChild<QWebEngineView *>();
            QVERIFY(input); QVERIFY(browser);
            QVERIFY(panel->rect().contains(input->geometry()));
            QVERIFY(panel->rect().contains(browser->geometry()));
            QVERIFY(input->y() >= browser->geometry().bottom());
        }
        host.tool->hide();
        host.tool->show();
        QTRY_COMPARE(panel->geometry(), host.tool->contentsRect());
        delete pluginView;
        QVERIFY(!host.tool);
        delete plugin;
    }

    void startupKeepsHostUntouchedUntilPanelIsShown_data()
    {
        QTest::addColumn<bool>("restoredVisible");
        QTest::newRow("hidden") << false;
        QTest::newRow("restored-visible") << true;
    }

    void startupKeepsHostUntouchedUntilPanelIsShown()
    {
        QFETCH(bool, restoredVisible);
        QWidget window;
        auto layout = new QVBoxLayout(&window);
        auto sidebar = new QPushButton(QStringLiteral("Sidebar control"), &window);
        layout->addWidget(sidebar);
        Host host; host.toolParent = &window;
        KTextEditor::MainWindow mainWindow(&host);
        QPluginLoader loader(QStringLiteral(PLUGIN_FILE));
        auto factory = qobject_cast<KPluginFactory *>(loader.instance()); QVERIFY(factory);
        auto plugin = factory->create<KTextEditor::Plugin>(&host); QVERIFY(plugin);
        const auto applicationPalette = QApplication::palette();
        auto pluginView = plugin->createView(&mainWindow);
        auto cleanup = qScopeGuard([&] { delete pluginView; delete plugin; });
        QVERIFY(host.tool);
        QVERIFY2(!host.tool->findChild<QWebEngineView *>(), "Loading a hidden plugin must not initialize WebEngine during Kate startup");
        QVERIFY(!window.windowHandle());
        QTest::qWait(25);
        QVERIFY(!host.tool->findChild<QWebEngineView *>());
        if (restoredVisible) host.showToolView(host.tool);
        auto dark = window.palette();
        dark.setColor(QPalette::Window, QColor(QStringLiteral("#31363b")));
        dark.setColor(QPalette::Base, QColor(QStringLiteral("#232629")));
        dark.setColor(QPalette::Text, QColor(QStringLiteral("#eff0f1")));
        dark.setColor(QPalette::WindowText, QColor(QStringLiteral("#eff0f1")));
        window.setPalette(dark);
        window.resize(900, 450); window.show();
        if (!restoredVisible) {
            QVERIFY(QTest::qWaitForWindowExposed(&window));
            window.activateWindow();
            QVERIFY(QTest::qWaitForWindowActive(&window));
            QVERIFY(!host.tool->findChild<QWebEngineView *>());
            connect(sidebar, &QPushButton::clicked, &host, [&] { host.showToolView(host.tool); });
            QTest::mouseClick(sidebar, Qt::LeftButton);
        }
        // Session restoration may show the tool before Kate finishes applying its theme.
        QVERIFY(!host.tool->findChild<QWebEngineView *>());
        QVERIFY(QTest::qWaitForWindowExposed(&window));
        QTRY_VERIFY(host.tool->findChild<QWebEngineView *>());
        auto browser = host.tool->findChild<QWebEngineView *>();
        QVERIFY(waitForScript(browser, QStringLiteral("getComputedStyle(document.body).backgroundColor === 'rgb(35, 38, 41)'")));
        if (!restoredVisible) {
            window.activateWindow();
            QVERIFY(QTest::qWaitForWindowActive(&window));
            QTRY_VERIFY(host.tool->findChild<QPlainTextEdit *>()->hasFocus());
        }
        QCOMPARE(QApplication::palette(), applicationPalette);
        QCOMPARE(window.palette(), dark);
        QVERIFY(sidebar->isVisible());
        QVERIFY(window.rect().contains(sidebar->geometry()));
        QVERIFY(!sidebar->geometry().intersects(host.tool->geometry()));
        QSignalSpy clicked(sidebar, &QPushButton::clicked);
        QTest::mouseClick(sidebar, Qt::LeftButton);
        QCOMPARE(clicked.count(), 1);
    }

    void escapeClosesPanelAndPreservesState_data()
    {
        QTest::addColumn<QString>("focusTarget");
        for (const auto name : {"composer", "transcript", "workspace", "pending"})
            QTest::newRow(name) << QString::fromLatin1(name);
    }

    void escapeClosesPanelAndPreservesState()
    {
        QFETCH(QString, focusTarget);
        Host host;
        KTextEditor::MainWindow mainWindow(&host);
        QWidget window; auto layout = new QVBoxLayout(&window);
        host.toolParent = &window;
        auto document = KTextEditor::Editor::instance()->createDocument(&window);
        QVERIFY(document->setText(QStringLiteral("Keep this editor context")));
        host.view = document->createView(&window, &mainWindow);
        layout->addWidget(host.view);
        QPluginLoader loader(QStringLiteral(PLUGIN_FILE));
        auto factory = qobject_cast<KPluginFactory *>(loader.instance()); QVERIFY(factory);
        auto plugin = factory->create<KTextEditor::Plugin>(&host); QVERIFY(plugin);
        auto pluginView = plugin->createView(&mainWindow);
        auto cleanup = qScopeGuard([&] { delete pluginView; delete plugin; });
        auto client = dynamic_cast<KXMLGUIClient *>(pluginView); QVERIFY(client);
        window.resize(900, 700); window.show(); window.activateWindow();
        QVERIFY(QTest::qWaitForWindowExposed(&window));
        client->actionCollection()->action(QStringLiteral("agent_pending_cursor"))->trigger();
        QTRY_VERIFY(host.tool->findChild<QWebEngineView *>());
        auto browser = host.tool->findChild<QWebEngineView *>();
        auto input = host.tool->findChild<QPlainTextEdit *>();
        auto pending = host.tool->findChild<QListWidget *>();
        QVERIFY(input); QVERIFY(pending);
        QVERIFY(waitForScript(browser, QStringLiteral("typeof agentPanelReceive === 'function'")));
        input->setPlainText(QStringLiteral("Keep this draft"));
        // Escape in an owned dialog must dismiss the dialog, leaving the panel open.
        if (focusTarget == QLatin1String("composer")) {
            QDialog dialog(host.tool);
            dialog.setModal(true);
            dialog.show(); dialog.activateWindow();
            QVERIFY(QTest::qWaitForWindowActive(&dialog));
            QTest::keyClick(&dialog, Qt::Key_Escape);
            QTRY_VERIFY(!dialog.isVisible());
            QCOMPARE(host.hideCount, 0);
            QVERIFY(host.tool->isVisible());
        }
        QWidget *target = input;
        if (focusTarget == QLatin1String("transcript")) target = browser;
        else if (focusTarget == QLatin1String("workspace")) target = host.tool->findChild<QLineEdit *>();
        else if (focusTarget == QLatin1String("pending")) target = pending;
        QVERIFY(target);
        QVERIFY(target->isVisible());
        window.activateWindow();
        QVERIFY(QTest::qWaitForWindowActive(&window));
        target->setFocus();
        QTRY_VERIFY(target->hasFocus());
        QTest::keyClick(QApplication::focusWidget(), Qt::Key_Escape);
        QTRY_COMPARE(host.hideCount, 1);
        QVERIFY(!host.tool->isVisible());
        QTRY_VERIFY(host.view->hasFocus());
        client->actionCollection()->action(QStringLiteral("agent_panel_show"))->trigger();
        QTRY_VERIFY(host.tool->isVisible());
        QTRY_VERIFY(input->hasFocus());
        QCOMPARE(host.tool->findChild<QWebEngineView *>(), browser);
        QCOMPARE(input->toPlainText(), QStringLiteral("Keep this draft"));
        QCOMPARE(pending->count(), 1);
    }

    void pluginIconWithAndWithoutTheme_data()
    {
        QTest::addColumn<QString>("theme");
        QTest::newRow("breeze") << QStringLiteral("breeze");
        QTest::newRow("breeze-dark") << QStringLiteral("breeze-dark");
        QTest::newRow("missing-theme") << QString();
    }

    void pluginIconWithAndWithoutTheme()
    {
        QFETCH(QString, theme);
        const auto originalTheme = QIcon::themeName();
        const auto originalFallback = QIcon::fallbackThemeName();
        const auto originalPaths = QIcon::themeSearchPaths();
        const auto originalFallbackPaths = QIcon::fallbackSearchPaths();
        auto restore = qScopeGuard([&] {
            QIcon::setThemeSearchPaths(originalPaths);
            QIcon::setFallbackSearchPaths(originalFallbackPaths);
            QIcon::setFallbackThemeName(originalFallback);
            QIcon::setThemeName(originalTheme);
        });
        QTemporaryDir emptyTheme;
        if (theme.isEmpty()) {
            QIcon::setThemeSearchPaths({emptyTheme.path()});
            QIcon::setFallbackSearchPaths({emptyTheme.path()});
            QIcon::setFallbackThemeName(QString());
            QIcon::setThemeName(QStringLiteral("missing-agent-test-theme"));
        } else {
            QIcon::setThemeName(theme);
        }
        Host host;
        KTextEditor::MainWindow mainWindow(&host);
        QPluginLoader loader(QStringLiteral(PLUGIN_FILE));
        auto factory = qobject_cast<KPluginFactory *>(loader.instance());
        QVERIFY2(factory, qPrintable(loader.errorString()));
        auto plugin = factory->create<KTextEditor::Plugin>(&host);
        QVERIFY(plugin);
        auto pluginView = plugin->createView(&mainWindow);
        auto cleanup = qScopeGuard([&] { delete pluginView; delete plugin; });
        QVERIFY2(!host.toolIcon.isNull(), "The Agent tab needs an icon, even without an icon theme");
        QVERIFY(!host.toolIcon.pixmap(16, 16).isNull());
        QVERIFY(!host.toolIcon.pixmap(32, 32).isNull());
    }

    void editorContextAndRenderedRoundTrip()
    {
        qputenv("KATE_AGENT_PI_COMMAND", QByteArray("python3 ") + SOURCE_DIR + "/tests/fake_agent.py");
        QTemporaryDir temp;
        const auto workspace = temp.path() + QStringLiteral("/project");
        QDir().mkpath(workspace + QStringLiteral("/src"));
        QDir().mkpath(workspace + QStringLiteral("/.git"));
        const auto filename = workspace + QStringLiteral("/src/test.cpp");
        QFile file(filename); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("saved\n"); file.close();
        QObject project; project.setProperty("projectBaseDir", workspace);
        Host host; host.project = &project;
        KTextEditor::MainWindow mainWindow(&host);
        QWidget editorParent;
        auto document = KTextEditor::Editor::instance()->createDocument(&editorParent);
        QVERIFY(document->openUrl(QUrl::fromLocalFile(filename)));
        QStringList lines;
        for (int i = 1; i <= 300; ++i) lines << QStringLiteral("line %1").arg(i);
        QVERIFY(document->setText(lines.join(QLatin1Char('\n'))));
        host.view = document->createView(&editorParent, &mainWindow);
        AgentPanel panel(&mainWindow);
        panel.resize(1100, 450); panel.show();
        panel.findChild<QLineEdit *>()->setText(workspace);
        auto pending = panel.findChild<QListWidget *>();
        QVERIFY(!pending->isVisible());
        auto input = panel.findChild<QPlainTextEdit *>();
        auto agents = panel.findChild<QComboBox *>();
        QTRY_VERIFY_WITH_TIMEOUT(agents->count() >= 7, 10000);
        agents->setCurrentIndex(agents->findData(QStringLiteral("pi")));

        // Exclusive selection end at column zero must produce L123-234, not L123-235.
        host.view->setSelection(KTextEditor::Range(122, 0, 234, 0));
        panel.copyReference(host.view);
        QCOMPARE(QApplication::clipboard()->text(), QStringLiteral("project/src/test.cpp:L123-234"));
        panel.addContext(host.view, true);
        QCOMPARE(pending->count(), 1);
        QVERIFY(pending->isVisible());
        QVERIFY(pending->item(0)->text().contains(QStringLiteral("project/src/test.cpp:L123-234")));
        const auto snapshot = QJsonDocument::fromJson(pending->item(0)->data(Qt::UserRole).toString().toUtf8()).object();
        QVERIFY(snapshot[QStringLiteral("unsaved")].toBool());
        QVERIFY(snapshot[QStringLiteral("text")].toString().startsWith(QStringLiteral("line 123")));
        panel.openLink(QStringLiteral("project/src/test.cpp:L123-234"));
        QCOMPARE(host.opened, QUrl::fromLocalFile(filename));
        QCOMPARE(host.view->selectionRange().start().line(), 122);
        QCOMPARE(host.view->selectionRange().end().line(), 233);

        auto browser = panel.findChild<QWebEngineView *>();
        bool ready = false;
        QTRY_VERIFY_WITH_TIMEOUT(([&] {
            browser->page()->runJavaScript(QStringLiteral("typeof agentPanelReceive === 'function'"), [&](const QVariant &v) { ready = v.toBool(); });
            return ready;
        })(), 15000);
        input->setPlainText(QStringLiteral("Render the selected context"));
        auto send = panel.findChild<QToolButton *>(QStringLiteral("agentSubmit"));
        QVERIFY(send); QTRY_VERIFY_WITH_TIMEOUT(send->isEnabled(), 5000);
        QTest::mouseClick(send, Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(send->text(), QStringLiteral("Send"), 10000);
        QCOMPARE(pending->count(), 0);
        QVERIFY(!pending->isVisible());
        bool rendered = false;
        QTRY_VERIFY_WITH_TIMEOUT(([&] {
            browser->page()->runJavaScript(QStringLiteral("!!document.querySelector('.assistant .katex') && !!document.querySelector('.assistant a.file-link')"),
                [&](const QVariant &v) { rendered = v.toBool(); });
            return rendered;
        })(), 10000);
        // Exercise a rendered link through the real WebChannel, not just the native slot.
        host.opened.clear();
        host.view->removeSelection();
        evaluate(browser, QStringLiteral("document.querySelector('.assistant a.file-link').click()"));
        QTRY_COMPARE(host.opened, QUrl::fromLocalFile(filename));
        QCOMPARE(host.view->selectionRange().start().line(), 122);
        QCOMPARE(host.view->selectionRange().end().line(), 233);
        // Allow Chromium's compositor to present the just-rendered DOM before capturing.
        QTest::qWait(250);
        panel.grab().save(QStringLiteral(SOURCE_DIR "/build/panel-preview.png"));
        panel.addContext(host.view, true);
        input->setPlainText(QStringLiteral("crash"));
        QTest::mouseClick(send, Qt::LeftButton);
        input->setPlainText(QStringLiteral("New draft"));
        auto restore = panel.findChild<QToolButton *>(QStringLiteral("agentRestoreInput"));
        QVERIFY(restore);
        QTRY_VERIFY_WITH_TIMEOUT(restore->isVisible(), 5000);
        panel.addContext(host.view, false);
        QTest::mouseClick(restore, Qt::LeftButton);
        QCOMPARE(input->toPlainText(), QStringLiteral("New draft\ncrash"));
        QCOMPARE(pending->count(), 2);
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length")).toInt(), 2);
    }

    void consoleLayoutFoldingAndPinnedInput()
    {
        Host host;
        KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow);
        panel.resize(960, 380); panel.show();
        auto browser = panel.findChild<QWebEngineView *>();
        auto input = panel.findChild<QPlainTextEdit *>();
        QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("typeof agentPanelReceive === 'function'")).toBool(), 15000);
        // Ensure the native channel is ready before injecting the conversation.
        QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("!!document.documentElement?.style.getPropertyValue('--font-size')")).toBool(), 5000);
        evaluate(browser, QStringLiteral(R"JS(
            agentPanelReceive({type: 'clear'});
            agentPanelReceive({type: 'message', id: 'user', role: 'user', text: 'Explain `project/src/main.cpp:L12-24`.'});
            agentPanelReceive({type: 'message', id: 'thought', role: 'reasoning', text: 'Inspect the selected function.'});
            agentPanelReceive({type: 'tool', id: 'read', title: 'Read src/main.cpp', status: 'running', text: 'File contents'});
            agentPanelReceive({type: 'message', id: 'log', role: 'diagnostic', text: 'Process log'});
            agentPanelReceive({type: 'message', id: 'reply', role: 'assistant', text: 'The selected function computes $E=mc^2$.\n\nSee `project/src/main.cpp:L12-24`.'});
        )JS"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("!!document.querySelector('.assistant .katex')")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral(R"JS(
            (() => {
                const user = document.querySelector('.user .body').getBoundingClientRect();
                const agent = document.querySelector('.assistant .body').getBoundingClientRect();
                return user.left === agent.left && user.right === agent.right &&
                    document.querySelector('#conversation').getBoundingClientRect().left === 0 &&
                    document.querySelectorAll('.process').length === 1 &&
                    !document.querySelector('.process').open &&
                    [...document.querySelectorAll('.reasoning,.tool,.diagnostic')].every(el => !el.checkVisibility());
            })()
        )JS")).toBool());
        QTest::qWait(100);
        panel.grab().save(QStringLiteral(SOURCE_DIR "/build/panel-console-preview.png"));

        evaluate(browser, QStringLiteral("document.querySelector('.process summary').click()"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.reasoning,.tool,.diagnostic')].every(el => el.checkVisibility())")).toBool());
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'tool', id:'read', title:'Read src/main.cpp', status:'completed', text:'Done'})"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.tool .body').textContent === 'Done'")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("document.querySelector('.process').open")).toBool());
        evaluate(browser, QStringLiteral("document.querySelector('.process summary').click()"));
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'message', id:'thought', role:'reasoning', append:true, text:' Additional reasoning.'})"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.reasoning .body').textContent.includes('Additional reasoning')")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.process').open && !document.querySelector('.reasoning').checkVisibility()")).toBool());

        // Existing views must adopt a changed Kate palette/font without reloading history.
        auto palette = panel.palette();
        palette.setColor(QPalette::Base, QColor(QStringLiteral("#232629")));
        palette.setColor(QPalette::Text, QColor(QStringLiteral("#eff0f1")));
        palette.setColor(QPalette::Window, QColor(QStringLiteral("#31363b")));
        palette.setColor(QPalette::WindowText, QColor(QStringLiteral("#eff0f1")));
        palette.setColor(QPalette::Button, QColor(QStringLiteral("#31363b")));
        palette.setColor(QPalette::ButtonText, QColor(QStringLiteral("#eff0f1")));
        palette.setColor(QPalette::AlternateBase, QColor(QStringLiteral("#31363b")));
        palette.setColor(QPalette::PlaceholderText, QColor(QStringLiteral("#a1a9b1")));
        palette.setColor(QPalette::Link, QColor(QStringLiteral("#3daee9")));
        panel.setPalette(palette);
        QFont font = panel.font(); font.setPixelSize(14); panel.setFont(font);
        QTRY_VERIFY(evaluate(browser, QStringLiteral("getComputedStyle(document.body).backgroundColor === 'rgb(35, 38, 41)' && getComputedStyle(document.body).fontSize === '14px'")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("document.querySelector('.assistant .katex') !== null")).toBool());
        QTest::qWait(100);
        panel.grab().save(QStringLiteral(SOURCE_DIR "/build/panel-console-dark-preview.png"));

        const auto inputRect = input->geometry();
        evaluate(browser, QStringLiteral(R"JS(
            for (let i = 0; i < 35; i++) {
                agentPanelReceive({type:'message', id:'u'+i, role:'user', text:'Question '+i});
                agentPanelReceive({type:'message', id:'t'+i, role:'reasoning', text:'Hidden reasoning'});
                agentPanelReceive({type:'message', id:'a'+i, role:'assistant', text:'Answer '+i});
            }
        )JS"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.assistant:last-child .body').textContent.trim() === 'Answer 34'")).toBool());
        evaluate(browser, QStringLiteral("window.scrollTo(0, document.documentElement.scrollHeight)"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("window.scrollY > 0")).toBool());
        QCOMPARE(input->geometry(), inputRect);
        QVERIFY(panel.rect().contains(input->geometry()));
        QVERIFY(input->isVisible());
        evaluate(browser, QStringLiteral("window.scrollTo(0, 0)"));
        QTRY_COMPARE(evaluate(browser, QStringLiteral("window.scrollY")).toInt(), 0);
        QTest::qWait(100); // Chromium delivers the scroll event asynchronously.
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'message', id:'a34', role:'assistant', append:true, text:' More output.'})"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.assistant:last-child .body').textContent.trim().endsWith('More output.')")).toBool());
        QCOMPARE(evaluate(browser, QStringLiteral("window.scrollY")).toInt(), 0);
        QCOMPARE(input->geometry(), inputRect);
        QVERIFY(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.process')].every(el => !el.open)")).toBool());
        input->setFocus();
        QTest::keyClicks(input, "Next question");
        QCOMPARE(input->toPlainText(), QStringLiteral("Next question"));
    }

    void renderedPathsAndMath()
    {
        QTemporaryDir temp;
        const auto workspace = temp.path() + QStringLiteral("/project");
        QDir().mkpath(workspace + QStringLiteral("/src"));
        const auto filename = workspace + QStringLiteral("/src/\u4e2d\u6587 file%20.cpp");
        const auto secondFile = workspace + QStringLiteral("/src/\u4e2d\u6587.cpp");
        QStringList lines;
        for (int i = 1; i <= 240; ++i) lines << QStringLiteral("line %1").arg(i);
        for (const auto &name : {filename, secondFile}) {
            QFile file(name); QVERIFY(file.open(QIODevice::WriteOnly)); file.write(lines.join(QLatin1Char('\n')).toUtf8());
        }
        Host host;
        KTextEditor::MainWindow mainWindow(&host);
        QWidget editorParent;
        auto document = KTextEditor::Editor::instance()->createDocument(&editorParent);
        QVERIFY(document->openUrl(QUrl::fromLocalFile(filename)));
        host.view = document->createView(&editorParent, &mainWindow);
        AgentPanel panel(&mainWindow); panel.resize(1000, 700); panel.show();
        panel.findChild<QLineEdit *>()->setText(workspace);
        auto browser = panel.findChild<QWebEngineView *>();
        QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("!!document.documentElement?.style.getPropertyValue('--font-size')")).toBool(), 15000);
        const auto source = QStringLiteral(R"MD(Inline: $E=mc^2$ and \( x^2 + y^2 \).
$$
\int_0^1 x^2\,dx=\frac{1}{3}
$$

\[\begin{pmatrix}1&2\\3&4\end{pmatrix}\]

$$
\begin{aligned}a&=b+c\\d&=e\end{aligned}
$$

`project/src/{unicode} file%20.cpp:L123-234`

[`project/src/{unicode} file%20.cpp`](project/src/%E4%B8%AD%E6%96%87%20file%2520.cpp#L7C3)

project/src/{unicode}.cpp:L3-9

`$code stays literal$`

```latex
$also literal$
```
)MD").replace(QStringLiteral("{unicode}"), QStringLiteral("\u4e2d\u6587"));
        const QJsonObject message{{QStringLiteral("type"), QStringLiteral("message")}, {QStringLiteral("id"), QStringLiteral("math-paths")},
            {QStringLiteral("role"), QStringLiteral("assistant")}, {QStringLiteral("text"), source}};
        evaluate(browser, QStringLiteral("agentPanelReceive(%1)").arg(QString::fromUtf8(QJsonDocument(message).toJson(QJsonDocument::Compact))));
        QTRY_COMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.assistant .katex').length")).toInt(), 5);
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.katex-display').length")).toInt(), 3);
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.assistant a').length")).toInt(), 3);
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.katex-error') && !document.querySelector('pre .katex') && !document.querySelector('a a')")).toBool());
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.fonts.check('16px KaTeX_Main') && document.fonts.check('italic 16px KaTeX_Math')")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.katex-html')].every(el => el.checkVisibility() && el.getBoundingClientRect().height > 0)")).toBool());
        QTest::qWait(100);
        panel.grab().save(QStringLiteral(SOURCE_DIR "/build/math-paths-preview.png"));

        evaluate(browser, QStringLiteral("document.querySelectorAll('.assistant a')[0].click()"));
        QTRY_COMPARE(host.opened, QUrl::fromLocalFile(filename));
        QCOMPARE(host.view->selectionRange().start().line(), 122);
        QCOMPARE(host.view->selectionRange().end().line(), 233);
        host.opened.clear();
        evaluate(browser, QStringLiteral("document.querySelectorAll('.assistant a')[1].click()"));
        QTRY_COMPARE(host.opened, QUrl::fromLocalFile(filename));
        QVERIFY(!host.view->selection());
        QCOMPARE(host.view->cursorPosition(), KTextEditor::Cursor(6, 2));
        evaluate(browser, QStringLiteral("document.querySelectorAll('.assistant a')[2].click()"));
        QTRY_COMPARE(host.opened, QUrl::fromLocalFile(secondFile));
        QCOMPARE(document->url(), QUrl::fromLocalFile(secondFile));
        QCOMPARE(host.view->selectionRange().start().line(), 2);
        QCOMPARE(host.view->selectionRange().end().line(), 8);

        evaluate(browser, QStringLiteral(R"JS(agentPanelReceive({type:'message', id:'stream-math', role:'assistant', text:'Streaming: $\\frac{1}{'}))JS"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.assistant:last-child .body').textContent.includes('Streaming:')")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.assistant:last-child .katex')")).toBool());
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'message', id:'stream-math', role:'assistant', append:true, text:'2}$'})"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("!!document.querySelector('.assistant:last-child .katex') && !document.querySelector('.katex-error')")).toBool());
    }

    void liveConversation()
    {
        const auto agent = qEnvironmentVariable("KATE_AGENT_LIVE_TEST");
        if (agent.isEmpty()) QSKIP("Opt in with KATE_AGENT_LIVE_TEST=deepseek or zcode; uses the existing model configuration.");
        QVERIFY(agent == QLatin1String("deepseek") || agent == QLatin1String("zcode"));
        QTemporaryDir temp;
        const auto workspace = temp.path() + QStringLiteral("/project");
        QDir().mkpath(workspace + QStringLiteral("/src"));
        QDir().mkpath(workspace + QStringLiteral("/.git"));
        const auto relative = QStringLiteral("src/\u4f8b\u5b50 file%20.cpp");
        const auto filename = workspace + QLatin1Char('/') + relative;
        const auto marker = QStringLiteral("KATE_") + QUuid::createUuid().toString(QUuid::Id128);
        const auto fileMarker = QStringLiteral("FILE_") + QUuid::createUuid().toString(QUuid::Id128);
        QStringList lines;
        for (int i = 1; i <= 240; ++i) lines << (i == 123 ? QStringLiteral("// ") + fileMarker : QStringLiteral("// line %1").arg(i));
        QFile file(filename); QVERIFY(file.open(QIODevice::WriteOnly));
        const auto original = lines.join(QLatin1Char('\n')).toUtf8();
        file.write(original); file.close();
        Host host;
        KTextEditor::MainWindow mainWindow(&host);
        QWidget editorParent;
        auto document = KTextEditor::Editor::instance()->createDocument(&editorParent);
        QVERIFY(document->openUrl(QUrl::fromLocalFile(filename)));
        host.view = document->createView(&editorParent, &mainWindow);
        AgentPanel panel(&mainWindow);
        panel.resize(1100, 650); panel.show();
        panel.findChild<QLineEdit *>()->setText(workspace);
        auto browser = panel.findChild<QWebEngineView *>();
        auto agents = panel.findChild<QComboBox *>();
        auto input = panel.findChild<QPlainTextEdit *>();
        QTRY_VERIFY_WITH_TIMEOUT(agents->count() >= 7, 10000);
        agents->setCurrentIndex(agents->findData(agent));
        auto send = panel.findChild<QToolButton *>(QStringLiteral("agentSubmit"));
        QVERIFY(send); QTRY_VERIFY_WITH_TIMEOUT(send->isEnabled(), 15000);
        QTRY_VERIFY_WITH_TIMEOUT(!panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"))->text().isEmpty(), 15000);
        QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("!!document.documentElement?.style.getPropertyValue('--font-size')")).toBool(), 15000);
        QSignalSpy events(&panel, &AgentPanel::transcriptEvent);
        const QStringList prompts = {
            QStringLiteral("Reply exactly %1. Remember this marker for my next question. Do not use tools.").arg(marker),
            QStringLiteral("Reply with the exact marker from my previous message and nothing else. Do not use tools."),
            QStringLiteral("Read only the file './%1' in this workspace using a file-reading tool. Do not read other files or modify anything. "
                "Reply with the FILE_ marker at line 123, then a clickable code reference `%2`, then inline math $E=mc^2$, "
                "then a display equation on separate lines:\n$$\n\\int_0^1 x^2\\,dx=\\frac{1}{3}\n$$\n"
                "Use the exact Markdown forms above, not a code fence.").arg(relative, QStringLiteral("project/") + relative + QStringLiteral(":L123-234"))
        };
        for (int turn = 0; turn < prompts.size(); ++turn) {
            events.clear();
            input->setPlainText(prompts[turn]);
            QTest::mouseClick(send, Qt::LeftButton);
            QTRY_COMPARE(send->text(), QStringLiteral("Stop"));
            QTRY_COMPARE_WITH_TIMEOUT(send->text(), QStringLiteral("Send"), 180000);
            QStringList errors;
            bool tool = false;
            for (const auto &record : events) {
                const auto event = QJsonDocument::fromJson(record.first().toString().toUtf8()).object();
                if (event[QStringLiteral("type")] == QLatin1String("error")) errors << event[QStringLiteral("text")].toString();
                if (event[QStringLiteral("type")] == QLatin1String("tool")) tool = true;
            }
            // Do not put raw agent diagnostics into test logs; they may contain configuration.
            QVERIFY2(errors.isEmpty(), "Real agent emitted an error; inspect the local panel without exposing credentials.");
            const auto expected = turn == 2 ? fileMarker : marker;
            QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.assistant > .body')].at(-1)?.textContent.includes('%1')").arg(expected)).toBool(), 10000);
            if (turn == 2) QVERIFY(tool);
            qInfo().noquote() << agent << "turn" << turn + 1 << "passed";
        }
        QTRY_VERIFY(evaluate(browser, QStringLiteral(R"JS(
            (() => {
                const reply = [...document.querySelectorAll('.assistant > .body')].at(-1);
                return reply.querySelectorAll('.katex').length >= 2 && !!reply.querySelector('.katex-display') &&
                    !reply.querySelector('.katex-error') && [...reply.querySelectorAll('.katex')].every(el => el.checkVisibility());
            })()
        )JS")).toBool());
        QTest::qWait(200);
        QVERIFY(panel.grab().save(QStringLiteral(SOURCE_DIR "/build/live-%1-preview.png").arg(agent)));
        evaluate(browser, QStringLiteral(R"JS(
            [...document.querySelectorAll('.assistant a')].find(el => {
                let href = el.getAttribute('href') || '';
                try { href = decodeURIComponent(href); } catch (_) {}
                return /[:#]L123-(?:L)?234\b/.test(el.textContent + ' ' + href);
            })?.click();
        )JS"));
        QTRY_COMPARE(host.opened, QUrl::fromLocalFile(filename));
        QCOMPARE(host.view->selectionRange().start().line(), 122);
        QCOMPARE(host.view->selectionRange().end().line(), 233);
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length")).toInt(), 3);
        QVERIFY(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.process')].every(el => !el.open)")).toBool());
        QVERIFY(file.open(QIODevice::ReadOnly)); QCOMPARE(file.readAll(), original);
    }

    void liveQuestionsAndResume()
    {
        const auto agent = qEnvironmentVariable("KATE_AGENT_LIVE_TEST");
        if (agent.isEmpty()) QSKIP("Opt in with KATE_AGENT_LIVE_TEST=deepseek or zcode; consumes model quota.");
        QVERIFY(agent == QLatin1String("deepseek") || agent == QLatin1String("zcode"));
        QTemporaryDir temp;
        const auto filename = temp.filePath(QStringLiteral("test.cpp"));
        QFile file(filename); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("// Live session test\n"); file.close();
        Host host; KTextEditor::MainWindow mainWindow(&host); QWidget editorParent;
        auto document = KTextEditor::Editor::instance()->createDocument(&editorParent);
        QVERIFY(document->openUrl(QUrl::fromLocalFile(filename)));
        host.view = document->createView(&editorParent, &mainWindow);
        const auto marker = QStringLiteral("MEMORY_") + QUuid::createUuid().toString(QUuid::Id128);
        auto send = [](AgentPanel &panel, const QString &text) {
            auto input = panel.findChild<QPlainTextEdit *>(); input->setPlainText(text);
            QTest::keyClick(input, Qt::Key_Return);
        };
        {
            AgentPanel panel(&mainWindow); panel.resize(1000, 600); panel.show();
            auto browser = panel.findChild<QWebEngineView *>();
            auto agents = panel.findChild<QComboBox *>();
            auto model = panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
            QVERIFY(waitForScript(browser, QStringLiteral("typeof nativePanel !== 'undefined'")));
            QTRY_VERIFY(agents->count() >= 7 && model->isEnabled());
            agents->setCurrentIndex(agents->findData(agent));
            send(panel, QStringLiteral("Remember marker %1. Reply only with that marker. Do not use tools.").arg(marker));
            QTRY_VERIFY_WITH_TIMEOUT(model->isEnabled(), 180000);
            QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.assistant .body')?.textContent.includes('%1')").arg(marker)).toBool());
            send(panel, QStringLiteral("Use your %1 tool to ask one single-choice question with options Alpha and Beta. Wait for my answer, then reply only with the chosen label. Do not use other tools or change files.")
                .arg(agent == QLatin1String("zcode") ? QStringLiteral("AskUserQuestion") : QStringLiteral("ask_user_question")));
            QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.request .choice')].some(el => el.textContent.includes('Beta'))")).toBool(), 180000);
            QVERIFY(!model->isEnabled());
            evaluate(browser, QStringLiteral(R"JS(
                [...document.querySelectorAll('.request .choice')].find(el => el.textContent.includes('Beta')).querySelector('input').click();
                [...document.querySelectorAll('.request button')].find(el => el.textContent === 'Reply').click();
            )JS"));
            QTRY_VERIFY_WITH_TIMEOUT(model->isEnabled(), 180000);
            QVERIFY(waitForScript(browser, QStringLiteral("[...document.querySelectorAll('.assistant .body')].at(-1)?.textContent.includes('Beta')")));
            QVERIFY(evaluate(browser, QStringLiteral("document.querySelectorAll('.error').length === 0")).toBool());
            QVERIFY(panel.grab().save(QStringLiteral(SOURCE_DIR "/build/live-%1-question-preview.png").arg(agent)));
        }
        AgentPanel restored(&mainWindow); restored.resize(1000, 600); restored.show();
        auto browser = restored.findChild<QWebEngineView *>();
        auto model = restored.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
        QTRY_VERIFY_WITH_TIMEOUT(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length === 2")).toBool(), 15000);
        QCOMPARE(restored.findChild<QComboBox *>()->currentData().toString(), agent);
        send(restored, QStringLiteral("What exact MEMORY_ marker did I give you? Reply only with that marker. Do not use tools."));
        QTRY_VERIFY_WITH_TIMEOUT(model->isEnabled(), 180000);
        QVERIFY(waitForScript(browser, QStringLiteral("[...document.querySelectorAll('.assistant .body')].at(-1)?.textContent.includes('%1')").arg(marker)));
        QVERIFY(evaluate(browser, QStringLiteral("document.querySelectorAll('.error').length === 0")).toBool());
    }

    void composerKeysHeightAndModelFields()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow); panel.resize(900, 450); panel.show(); panel.activateWindow();
        auto browser = panel.findChild<QWebEngineView *>();
        auto input = panel.findChild<QPlainTextEdit *>(QStringLiteral("agentComposer"));
        auto model = panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
        auto effort = panel.findChild<QLineEdit *>(QStringLiteral("agentReasoningEffort"));
        QVERIFY(model); QVERIFY(effort); QVERIFY(!model->completer()); QVERIFY(!effort->completer());
        QVERIFY(waitForScript(browser, QStringLiteral("typeof nativePanel !== 'undefined'")));
        auto agents = panel.findChild<QComboBox *>();
        QTRY_VERIFY(agents->count() >= 7 && model->isEnabled());
        agents->setCurrentIndex(agents->findData(QStringLiteral("pi")));
        model->setText(QStringLiteral("test-model")); effort->setText(QStringLiteral("high"));
        input->setFocus(); QTRY_VERIFY(input->hasFocus());
        const int single = input->height();
        QCOMPARE(input->verticalScrollBar()->maximum(), 0);
        QVERIFY(single < input->fontMetrics().lineSpacing() * 2 + 10);
        QTest::keyClicks(input, "First line");
        QTest::keyClick(input, Qt::Key_Return, Qt::ControlModifier);
        QCOMPARE(input->toPlainText(), QStringLiteral("First line\n"));
        QTRY_VERIFY(input->height() > single);
        const int doubled = input->height();
        QTest::keyClicks(input, "Second line");
        QTest::keyClick(input, Qt::Key_Return, Qt::ControlModifier);
        QTest::keyClicks(input, "Third line");
        QCOMPARE(input->height(), doubled);
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length")).toInt(), 0);
        QTest::keyClick(input, Qt::Key_Return);
        QTRY_VERIFY(input->toPlainText().isEmpty());
        QTRY_COMPARE(input->height(), single);
        QVERIFY(waitForScript(browser, QStringLiteral("document.querySelector('.assistant .body')?.textContent.includes('Third line')")));
        QTRY_VERIFY(model->isEnabled());
        input->setPlainText(QString(1000, QLatin1Char('x')));
        QTRY_COMPARE(input->height(), doubled);
        input->clear(); QTRY_COMPARE(input->height(), single);
        // An IME's Enter confirms preedit text; it must never submit a turn.
        input->setPlainText(QStringLiteral("Draft"));
        QInputMethodEvent preedit(QStringLiteral("composition"), {});
        QApplication::sendEvent(input, &preedit);
        QTest::keyClick(input, Qt::Key_Return);
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length")).toInt(), 1);
        QInputMethodEvent commit; commit.setCommitString(QStringLiteral(" committed"));
        QApplication::sendEvent(input, &commit);
        QVERIFY(!input->toPlainText().isEmpty());
    }

    void effectiveConfigurationUpdatesBeforeSending()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow); panel.resize(1000, 400); panel.show();
        auto model = panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
        auto effort = panel.findChild<QLineEdit *>(QStringLiteral("agentReasoningEffort"));
        auto browser = panel.findChild<QWebEngineView *>();
        QTRY_COMPARE(model->text(), QStringLiteral("fake-model"));
        QTRY_VERIFY(model->isEnabled());
        QCOMPARE(effort->text(), QStringLiteral("off"));
        panel.activateWindow();
        model->setFocus(); QTRY_VERIFY(model->hasFocus());
        model->selectAll(); QTest::keyClicks(model, "other-model");
        QTest::keyClick(model, Qt::Key_Tab);
        QVERIFY(!model->isEnabled());
        QTRY_VERIFY(model->isEnabled());
        QCOMPARE(model->text(), QStringLiteral("other-model"));
        effort->setFocus(); QTRY_VERIFY(effort->hasFocus()); effort->selectAll(); QTest::keyClicks(effort, "high");
        QTest::keyClick(effort, Qt::Key_Tab);
        QVERIFY(!effort->isEnabled());
        QTRY_VERIFY(effort->isEnabled());
        QCOMPARE(effort->text(), QStringLiteral("high"));
        QVERIFY(waitForScript(browser, QStringLiteral("typeof nativePanel !== 'undefined'")));
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length")).toInt(), 0);
        model->setFocus(); QTRY_VERIFY(model->hasFocus()); model->selectAll(); QTest::keyClicks(model, "missing-model");
        QTest::keyClick(model, Qt::Key_Tab);
        QVERIFY(!model->isEnabled());
        QTRY_VERIFY(model->isEnabled());
        QTRY_COMPARE(model->text(), QStringLiteral("other-model"));
        QVERIFY(waitForScript(browser, QStringLiteral("!!document.querySelector('.error')")));
    }

    void newSessionDirectoryUsesProjectFileThenHome()
    {
        QTemporaryDir temp;
        const auto projectPath = temp.path() + QStringLiteral("/project");
        const auto filePath = temp.path() + QStringLiteral("/files/example.cpp");
        QVERIFY(QDir().mkpath(projectPath));
        QVERIFY(QDir().mkpath(QFileInfo(filePath).absolutePath()));
        QFile file(filePath); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("// example\n"); file.close();
        QObject project; project.setProperty("projectBaseDir", projectPath);
        Host host; host.project = &project;
        KTextEditor::MainWindow mainWindow(&host);
        QWidget editorParent;
        auto document = KTextEditor::Editor::instance()->createDocument(&editorParent);
        QVERIFY(document->openUrl(QUrl::fromLocalFile(filePath)));
        host.view = document->createView(&editorParent, &mainWindow);
        AgentPanel panel(&mainWindow); panel.resize(1000, 400); panel.show();
        auto directory = panel.findChild<QLineEdit *>(QStringLiteral("agentWorkspace"));
        auto fresh = panel.findChild<QToolButton *>(QStringLiteral("agentNewChat"));
        auto model = panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
        QCOMPARE(directory->text(), projectPath);
        QTRY_VERIFY(model->isEnabled() && !model->text().isEmpty());
        directory->setFocus(); directory->selectAll(); QTest::keyClicks(directory, QDir::homePath());
        fresh->click();
        QCOMPARE(directory->text(), projectPath);
        host.project = nullptr;
        fresh->click();
        QCOMPARE(directory->text(), QFileInfo(filePath).absolutePath());
        host.view = nullptr;
        fresh->click();
        QCOMPARE(directory->text(), QDir::homePath());
        QTRY_VERIFY(model->isEnabled());
    }

    void inlineSendStopAndAnimatedTurnSummary()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow); panel.resize(1000, 400); panel.show();
        auto input = panel.findChild<QPlainTextEdit *>();
        auto submit = panel.findChild<QToolButton *>(QStringLiteral("agentSubmit"));
        auto model = panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
        auto browser = panel.findChild<QWebEngineView *>();
        QTRY_VERIFY(model->isEnabled() && !model->text().isEmpty());
        QVERIFY(waitForScript(browser, QStringLiteral("typeof nativePanel !== 'undefined'")));
        QVERIFY(submit->x() > input->geometry().right());
        QCOMPARE(submit->geometry().bottom(), input->geometry().bottom());
        QVERIFY(panel.height() - input->geometry().bottom() <= 6);
        QVERIFY(!submit->icon().isNull());
        input->setPlainText(QStringLiteral("hang")); QTest::keyClick(input, Qt::Key_Return);
        QTRY_COMPARE(submit->text(), QStringLiteral("Stop"));
        QVERIFY(submit->isEnabled());
        QVERIFY(waitForScript(browser, QStringLiteral("document.querySelector('#conversation > :last-child')?.classList.contains('activity')")));
        const auto dots = evaluate(browser, QStringLiteral("document.querySelector('.activity-dots').textContent")).toString();
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.activity-dots').textContent")).toString() != dots);
        submit->click();
        QTRY_COMPARE(submit->text(), QStringLiteral("Send"));
        QVERIFY(waitForScript(browser, QStringLiteral("!document.querySelector('.activity') && document.querySelector('.turn-summary')?.textContent.includes('Stopped in')")));
        input->setPlainText(QStringLiteral("hello")); QTest::keyClick(input, Qt::Key_Return);
        QVERIFY(waitForScript(browser, QStringLiteral("document.querySelectorAll('.turn-summary').length === 2 && [...document.querySelectorAll('.turn-summary')].at(-1).textContent.includes('Completed in')")));
        QVERIFY(panel.grab().save(QStringLiteral(SOURCE_DIR "/build/configuration-preview.png")));
    }

    void localSessionsRestoreHistoryAndParameters()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        {
            AgentPanel panel(&mainWindow); panel.resize(900, 450); panel.show();
            auto browser = panel.findChild<QWebEngineView *>();
            auto agents = panel.findChild<QComboBox *>();
            auto model = panel.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
            QVERIFY(waitForScript(browser, QStringLiteral("typeof nativePanel !== 'undefined'")));
            QTRY_VERIFY(agents->count() >= 7 && model->isEnabled());
            agents->setCurrentIndex(agents->findData(QStringLiteral("pi")));
            model->setText(QStringLiteral("saved-model"));
            panel.findChild<QLineEdit *>(QStringLiteral("agentReasoningEffort"))->setText(QStringLiteral("low"));
            auto input = panel.findChild<QPlainTextEdit *>();
            input->setPlainText(QStringLiteral("Remember this conversation"));
            QTest::keyClick(input, Qt::Key_Return);
            QVERIFY(waitForScript(browser, QStringLiteral("document.querySelector('.assistant .body')?.textContent.includes('Remember this conversation')")));
            QTRY_VERIFY(model->isEnabled());
        }
        AgentPanel restored(&mainWindow); restored.resize(900, 450); restored.show();
        auto browser = restored.findChild<QWebEngineView *>();
        auto model = restored.findChild<QLineEdit *>(QStringLiteral("agentModelId"));
        QTRY_COMPARE(model->text(), QStringLiteral("saved-model"));
        QCOMPARE(restored.findChild<QLineEdit *>(QStringLiteral("agentReasoningEffort"))->text(), QStringLiteral("low"));
        QCOMPARE(restored.findChild<QComboBox *>()->currentData().toString(), QStringLiteral("pi"));
        QVERIFY(waitForScript(browser, QStringLiteral("document.querySelector('.assistant .body')?.textContent.includes('Remember this conversation') && document.querySelectorAll('.user').length === 1")));
        auto input = restored.findChild<QPlainTextEdit *>(); input->setPlainText(QStringLiteral("Keep draft"));
        auto sessions = restored.findChild<QToolButton *>(QStringLiteral("agentLocalSessions")); QVERIFY(sessions);
        bool checked = false;
        QTimer::singleShot(150, &restored, [&] {
            auto dialog = restored.findChild<QDialog *>();
            if (!dialog) return;
            auto close = qScopeGuard([&] { dialog->reject(); });
            auto view = dialog->findChild<QTreeWidget *>(QStringLiteral("agentSessionList"));
            QVERIFY(view); QCOMPARE(view->headerItem()->text(0), QStringLiteral("CLI Tool"));
            QTRY_COMPARE(view->topLevelItemCount(), 1);
            QCOMPARE(view->topLevelItem(0)->text(0), QStringLiteral("Pi"));
            QVERIFY(!view->topLevelItem(0)->isDisabled());
            checked = true;
        });
        sessions->click(); QVERIFY(checked);
        QCOMPARE(input->toPlainText(), QStringLiteral("Keep draft"));
        QVERIFY(restored.grab().save(QStringLiteral(SOURCE_DIR "/build/session-preview.png")));
    }

    void choiceFormsValidationRetryAndErrors()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow); panel.resize(900, 600); panel.show();
        auto browser = panel.findChild<QWebEngineView *>();
        QVERIFY(waitForScript(browser, QStringLiteral("typeof nativePanel !== 'undefined'")));
        evaluate(browser, QStringLiteral(R"JS(
            nativePanel = {reply: (id,value) => { window.lastAnswer = {id, value:JSON.parse(value)}; }};
            agentPanelReceive({type:'request',id:'choice',title:'Choose options',questions:[
                {id:'one',question:'Pick one',required:true,allowOther:false,options:[{label:'First',value:'a'},{label:'Second',value:'b'}]},
                {id:'many',question:'Pick several',required:true,multiSelect:true,options:[{label:'Alpha'},{label:'Beta'}]},
                {id:'secret',question:'Secret answer',isSecret:true,required:true}
            ]});
            document.querySelector('.request button').click();
        )JS"));
        QVERIFY(evaluate(browser, QStringLiteral("!window.lastAnswer && !!document.querySelector('.request-error') && document.querySelectorAll('input:checked').length === 0")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("document.querySelector('input[type=password]').autocomplete === 'off'")).toBool());
        evaluate(browser, QStringLiteral(R"JS(
            document.querySelector('input[type=radio]').click();
            document.querySelectorAll('input[type=checkbox]').forEach(input => input.click());
            document.querySelector('input[aria-label="Custom answer"]').value='Custom';
            document.querySelector('input[type=password]').value='test-only';
            document.querySelector('.request button').click();
        )JS"));
        QVERIFY(evaluate(browser, QStringLiteral("lastAnswer.value.one === 'a' && JSON.stringify(lastAnswer.value.many) === JSON.stringify(['Alpha','Beta','Custom']) && lastAnswer.value.secret === 'test-only'")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("[...document.querySelectorAll('.request input,.request button')].every(el => el.disabled)")).toBool());
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'request_failed',id:'choice',text:'Retry the answer'})"));
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.request button').disabled && document.querySelector('.request-error').textContent === 'Retry the answer'")).toBool());
        evaluate(browser, QStringLiteral("[...document.querySelectorAll('.request button')].find(el => el.textContent === 'Dismiss').click()"));
        QVERIFY(evaluate(browser, QStringLiteral("lastAnswer.value._cancel === true")).toBool());
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'request_resolved',id:'choice'}); agentPanelReceive({type:'error',text:'Provider quota exhausted'});"));
        QVERIFY(waitForScript(browser, QStringLiteral("document.querySelector('.error .body')?.textContent.includes('Provider quota exhausted')")));
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.error').closest('details') && document.querySelector('.request').classList.contains('resolved')")).toBool());
    }

    void permissionsAndReloadPreserveDraft()
    {
        const auto previous = qgetenv("KATE_AGENT_KIMI_COMMAND");
        const auto restoreEnvironment = qScopeGuard([previous] { qputenv("KATE_AGENT_KIMI_COMMAND", previous); });
        qputenv("KATE_AGENT_KIMI_COMMAND", QByteArray("python3 ") + SOURCE_DIR + "/tests/fake_protocol_agent.py acp");
        Host host; KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow); panel.resize(900, 450); panel.show();
        auto browser = panel.findChild<QWebEngineView *>();
        auto input = panel.findChild<QPlainTextEdit *>();
        auto agents = panel.findChild<QComboBox *>();
        QTRY_VERIFY_WITH_TIMEOUT(agents->count() >= 7, 10000);
        agents->setCurrentIndex(agents->findData(QStringLiteral("kimi")));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("typeof nativePanel !== 'undefined'")).toBool());
        panel.activateWindow();
        input->setFocus();
        QTRY_VERIFY(input->hasFocus());
        input->setPlainText(QStringLiteral("Permission round trip"));
        QTest::keyClick(input, Qt::Key_Return);
        QTRY_COMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.user').length")).toInt(), 1);
        QTRY_VERIFY(evaluate(browser, QStringLiteral("!!document.querySelector('.request button')")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.assistant')")).toBool());
        input->setPlainText(QStringLiteral("Unsent next question"));
        evaluate(browser, QStringLiteral("document.querySelector('.request button').click()"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("!!document.querySelector('.request.resolved button:disabled') && document.querySelector('.assistant .body')?.textContent.includes('approved answer')")).toBool());
        QCOMPARE(input->toPlainText(), QStringLiteral("Unsent next question"));
        QSignalSpy loaded(browser, &QWebEngineView::loadFinished);
        browser->reload();
        QVERIFY(loaded.wait(10000));
        QVERIFY(loaded.last().first().toBool());
        QVERIFY(waitForScript(browser, QStringLiteral("!!document.querySelector('.request.resolved button:disabled') && document.querySelectorAll('.user').length === 1 && document.querySelectorAll('.assistant').length === 1")));
        QCOMPARE(input->toPlainText(), QStringLiteral("Unsent next question"));
    }

    void untrustedOutputAndTranscriptRetention()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        AgentPanel panel(&mainWindow); panel.resize(900, 450); panel.show();
        auto browser = panel.findChild<QWebEngineView *>();
        QTRY_VERIFY(evaluate(browser, QStringLiteral("typeof nativePanel !== 'undefined'")).toBool());
        evaluate(browser, QStringLiteral(R"JS(
            agentPanelReceive({type:'message', id:'unsafe', role:'assistant', text:
                '<script>window.injected=true</script><img src=x onerror="window.injected=true">\n' +
                '[unsafe](javascript:alert(1)) ![remote](https://example.invalid/track)'});
            fetch('https://example.invalid/blocked').then(() => window.offlineResult='connected', () => window.offlineResult='blocked');
        )JS"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.assistant .body')?.textContent.includes('<script>')")).toBool());
        QVERIFY(evaluate(browser, QStringLiteral("!window.injected && !document.querySelector('#conversation script,#conversation img,#conversation iframe,a[href^=\"javascript:\"]')")).toBool());
        QTRY_COMPARE(evaluate(browser, QStringLiteral("window.offlineResult")).toString(), QStringLiteral("blocked"));
        QVERIFY(evaluate(browser, QStringLiteral("nativePanel.setStyleSheet === undefined && nativePanel.close === undefined")).toBool());
        evaluate(browser, QStringLiteral(R"JS(
            agentPanelReceive({type:'request', id:'stale', title:'Confirm', options:[{id:'yes',label:'Allow'}]});
            agentPanelReceive({type:'requests_cleared'});
            for (let i=0; i<1010; i++) agentPanelReceive({type:'message',id:'retained-'+i,role:'assistant',text:'Entry '+i});
        )JS"));
        QTRY_VERIFY(evaluate(browser, QStringLiteral("document.querySelector('.message:last-child .body')?.textContent.trim() === 'Entry 1009'")).toBool());
        QCOMPARE(evaluate(browser, QStringLiteral("document.querySelectorAll('.message').length")).toInt(), 1000);
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('.request') && document.querySelector('.message .body').textContent.trim() === 'Entry 10'")).toBool());
        evaluate(browser, QStringLiteral("agentPanelReceive({type:'clear'})"));
        QVERIFY(evaluate(browser, QStringLiteral("!document.querySelector('#conversation').childElementCount && !document.querySelector('#empty').hidden")).toBool());
    }

    void contextActionsUseClickedSplitAndCleanUp()
    {
        Host host; KTextEditor::MainWindow mainWindow(&host);
        QWidget editorParent;
        auto first = KTextEditor::Editor::instance()->createDocument(&editorParent);
        auto second = KTextEditor::Editor::instance()->createDocument(&editorParent);
        QVERIFY(first->setText(QStringLiteral("active view")));
        QVERIFY(second->setText(QStringLiteral("clicked view\nsecond line")));
        host.view = first->createView(&editorParent, &mainWindow);
        auto split = second->createView(&editorParent, &mainWindow);
        QPluginLoader loader(QStringLiteral(PLUGIN_FILE));
        auto factory = qobject_cast<KPluginFactory *>(loader.instance()); QVERIFY(factory);
        auto plugin = factory->create<KTextEditor::Plugin>(&host); QVERIFY(plugin);
        auto pluginView = plugin->createView(&mainWindow); QVERIFY(pluginView);
        auto client = dynamic_cast<KXMLGUIClient *>(pluginView); QVERIFY(client);
        auto selection = client->actionCollection()->action(QStringLiteral("agent_pending_selection")); QVERIFY(selection);
        QCOMPARE(selection->shortcut(), QKeySequence(QStringLiteral("Ctrl+Alt+S")));
        // The second view appears after the plugin has been created.
        Q_EMIT mainWindow.viewCreated(split);
        split->setSelection(KTextEditor::Range(0, 0, 0, 7));
        QMenu menu;
        Q_EMIT split->contextMenuAboutToShow(split, &menu);
        Q_EMIT split->contextMenuAboutToShow(split, &menu);
        QCOMPARE(menu.actions().size(), 1);
        auto submenu = menu.actions().first()->menu(); QVERIFY(submenu);
        submenu->actions().first()->trigger();
        auto pending = host.tool->findChild<QListWidget *>(); QVERIFY(pending);
        QCOMPARE(pending->count(), 1);
        auto snapshot = QJsonDocument::fromJson(pending->item(0)->data(Qt::UserRole).toString().toUtf8()).object();
        QCOMPARE(snapshot[QStringLiteral("text")].toString(), QStringLiteral("clicked"));
        QVERIFY(snapshot[QStringLiteral("unsaved")].toBool());
        QCOMPARE(host.shown, host.tool);
        host.view->setSelection(KTextEditor::Range(0, 0, 0, 6));
        selection->trigger();
        QCOMPARE(pending->count(), 2);
        snapshot = QJsonDocument::fromJson(pending->item(1)->data(Qt::UserRole).toString().toUtf8()).object();
        QCOMPARE(snapshot[QStringLiteral("text")].toString(), QStringLiteral("active"));
        split->removeSelection(); split->setCursorPosition(KTextEditor::Cursor(1, 3));
        Q_EMIT split->contextMenuAboutToShow(split, &menu);
        submenu = menu.actions().first()->menu();
        QVERIFY(!submenu->actions().first()->isEnabled());
        submenu->actions()[1]->trigger();
        snapshot = QJsonDocument::fromJson(pending->item(2)->data(Qt::UserRole).toString().toUtf8()).object();
        QCOMPARE(snapshot[QStringLiteral("line")].toInt(), 2);
        QCOMPARE(snapshot[QStringLiteral("column")].toInt(), 4);
        QCOMPARE(snapshot[QStringLiteral("text")].toString(), QStringLiteral("second line"));
        delete split;
        submenu->actions()[1]->trigger();
        QCOMPARE(pending->count(), 3);
        delete pluginView; QVERIFY(!host.tool); QVERIFY(menu.actions().isEmpty());
        delete plugin;
    }
};

int main(int argc, char **argv)
{
    QCoreApplication::setAttribute(Qt::AA_ShareOpenGLContexts);
    QApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("kate-agent-panel-test"));
    PanelTest test;
    return QTest::qExec(&test, argc, argv);
}
#include "panel_test.moc"
