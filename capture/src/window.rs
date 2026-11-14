//! The deep-memory window: a native window whose content is `ui.html` in a
//! WKWebView. The page talks to `api::handle` through a script message
//! handler, so no network port is opened and nothing outside this process
//! can reach the data.

use crate::api;
use objc2::rc::Retained;
use objc2::runtime::{NSObject, NSObjectProtocol, ProtocolObject};
use objc2::{define_class, msg_send, sel, DefinedClass, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSBackingStoreType, NSMenu, NSMenuItem, NSWindow, NSWindowStyleMask,
};
use objc2_foundation::{NSPoint, NSRect, NSSize, NSString};
use objc2_web_kit::{
    WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate, WKScriptMessage,
    WKScriptMessageHandler, WKUserContentController, WKWebView, WKWebViewConfiguration,
};

const PAGE: &str = include_str!("ui.html");

define_class!(
    // Receives the page's requests and keeps the web view on the embedded page.
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "DeepmemBridge"]
    #[ivars = api::Context]
    struct Bridge;

    unsafe impl NSObjectProtocol for Bridge {}

    unsafe impl WKScriptMessageHandler for Bridge {
        #[unsafe(method(userContentController:didReceiveScriptMessage:))]
        fn did_receive(&self, _controller: &WKUserContentController, message: &WKScriptMessage) {
            let Ok(request) = unsafe { message.body() }.downcast::<NSString>() else { return };
            let request = request.to_string();
            if std::env::var_os("DEEPMEM_DEBUG").is_some() {
                let cmd = request.split("\"cmd\":\"").nth(1).and_then(|rest| rest.split('"').next());
                eprintln!("window: {}", cmd.unwrap_or("?"));
            }
            let reply = api::handle(&request, self.ivars());
            if let Some(web) = unsafe { message.webView() } {
                // A JSON document is also a JavaScript expression.
                run_script(&web, &format!("deepmemReply({reply})"));
            }
        }
    }

    unsafe impl WKNavigationDelegate for Bridge {
        // Memory text is captured data and may contain links. Only the
        // embedded page (loaded as about:blank) is ever shown.
        #[unsafe(method(webView:decidePolicyForNavigationAction:decisionHandler:))]
        fn decide(
            &self,
            _web: &WKWebView,
            action: &WKNavigationAction,
            decision: &block2::DynBlock<dyn Fn(WKNavigationActionPolicy)>,
        ) {
            let url = unsafe { action.request().URL() }.and_then(|url| url.absoluteString());
            let embedded = url.is_some_and(|url| url.to_string() == "about:blank");
            decision.call((if embedded {
                WKNavigationActionPolicy::Allow
            } else {
                WKNavigationActionPolicy::Cancel
            },));
        }
    }
);

fn run_script(web: &WKWebView, script: &str) {
    unsafe { web.evaluateJavaScript_completionHandler(&NSString::from_str(script), None) };
}

pub struct Window {
    window: Retained<NSWindow>,
    web: Retained<WKWebView>,
    _bridge: Retained<Bridge>, // the web view holds its delegate weakly
}

impl Window {
    pub fn new(mtm: MainThreadMarker, ctx: api::Context) -> Self {
        let bridge: Retained<Bridge> =
            unsafe { msg_send![super(Bridge::alloc(mtm).set_ivars(ctx)), init] };
        let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(980.0, 660.0));
        let web = unsafe {
            let config = WKWebViewConfiguration::new(mtm);
            config
                .userContentController()
                .addScriptMessageHandler_name(ProtocolObject::from_ref(&*bridge), &NSString::from_str("deepmem"));
            let web = WKWebView::initWithFrame_configuration(WKWebView::alloc(mtm), frame, &config);
            web.setNavigationDelegate(Some(ProtocolObject::from_ref(&*bridge)));
            web.loadHTMLString_baseURL(&NSString::from_str(PAGE), None);
            web
        };
        let style = NSWindowStyleMask::Titled
            | NSWindowStyleMask::Closable
            | NSWindowStyleMask::Miniaturizable
            | NSWindowStyleMask::Resizable;
        let window = unsafe {
            let window = NSWindow::initWithContentRect_styleMask_backing_defer(
                NSWindow::alloc(mtm),
                frame,
                style,
                NSBackingStoreType::Buffered,
                false,
            );
            window.setReleasedWhenClosed(false); // closing hides it; `show` brings it back
            window
        };
        window.setTitle(&NSString::from_str("deep-memory"));
        window.setContentMinSize(NSSize::new(720.0, 440.0));
        window.setContentView(Some(&web));
        window.center();
        window.setFrameAutosaveName(&NSString::from_str("DeepmemWindow"));
        Window { window, web, _bridge: bridge }
    }

    pub fn show(&self, app: &NSApplication) {
        // An accessory app is never frontmost by itself.
        #[allow(deprecated)]
        app.activateIgnoringOtherApps(true);
        self.window.makeKeyAndOrderFront(None);
        // Files may have changed while the window was closed.
        run_script(&self.web, "window.deepmemShown && deepmemShown()");
    }
}

/// Text fields get Cut/Copy/Paste/Select All only through a main menu's key
/// equivalents, even in an app that never shows its menu bar. There is no
/// Cmd+Q on purpose: quitting stops capture, so it stays a menu bar click.
pub fn install_edit_menu(mtm: MainThreadMarker, app: &NSApplication) {
    let item = |title: &str, action, key: &str| unsafe {
        NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str(title),
            Some(action),
            &NSString::from_str(key),
        )
    };
    let edit = NSMenu::initWithTitle(NSMenu::alloc(mtm), &NSString::from_str("Edit"));
    edit.addItem(&item("Undo", sel!(undo:), "z"));
    edit.addItem(&item("Redo", sel!(redo:), "Z"));
    edit.addItem(&item("Cut", sel!(cut:), "x"));
    edit.addItem(&item("Copy", sel!(copy:), "c"));
    edit.addItem(&item("Paste", sel!(paste:), "v"));
    edit.addItem(&item("Select All", sel!(selectAll:), "a"));
    edit.addItem(&item("Close Window", sel!(performClose:), "w"));
    let holder = NSMenuItem::new(mtm);
    holder.setSubmenu(Some(&edit));
    let main = NSMenu::new(mtm);
    main.addItem(&holder);
    app.setMainMenu(Some(&main));
}
