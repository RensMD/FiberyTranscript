"""Fibery entity panel - embedded as a second WebView2 in the same WinForms form.

Uses a SplitContainer to host two WebView2 controls side-by-side inside the
existing pywebview BrowserForm. This gives a true single OS window with no
sync code needed: minimize/restore/close/maximize all work natively.

Left panel (main app):  fixed width, not resizable by the splitter.
Right panel (Fibery):   fills all remaining space; window expands to fit.
"""

import logging
import os
import sys

from config.constants import FIBERY_INSTANCE_URL

logger = logging.getLogger(__name__)

_PANEL_DEFAULT_WIDTH = 900
_MIN_PANEL_WIDTH = 900
_DEFAULT_URL = FIBERY_INSTANCE_URL


def _get_winforms_form(main_window):
    """Return the WinForms BrowserForm for the given pywebview window."""
    from webview.platforms.winforms import BrowserView
    return BrowserView.instances[main_window.uid]


def _get_cache_dir():
    """Return the webview cache directory set by start_webview()."""
    try:
        from webview.platforms.winforms import cache_dir
        return cache_dir
    except ImportError:
        return None


class EntityPanel:
    def __init__(self, main_window, settings=None):
        self._main = main_window
        self._settings = settings
        self._split = None          # WinForms SplitContainer
        self._panel_wv = None       # second WebView2 control
        self._original_width = None
        self._original_min_width = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_open(self):
        return self._split is not None

    def get_current_url(self) -> str:
        """Return the current URL displayed in the entity panel WebView2."""
        if self._panel_wv is None:
            return ""
        try:
            form = _get_winforms_form(self._main)
            from System import Func, Type
            result = [""]
            def _get():
                try:
                    if self._panel_wv.CoreWebView2 is not None:
                        result[0] = self._panel_wv.CoreWebView2.Source or ""
                except Exception:
                    logger.exception("get_current_url failed")
            form.Invoke(Func[Type](_get))
            return result[0]
        except Exception:
            logger.exception("get_current_url invoke failed")
            return ""

    def open(self, url: str):
        """Open (or navigate) the Fibery panel with the given URL."""
        if sys.platform != "win32":
            logger.warning("EntityPanel only supported on Windows")
            return

        if self._split is not None:
            # Already open - just navigate
            self._navigate(url)
            return

        form = _get_winforms_form(self._main)
        # Use Func[Type] matching pywebview's own Invoke pattern
        from System import Func, Type
        form.Invoke(Func[Type](lambda: self._build_panel(form, url)))

    def open_default(self):
        """Open the panel with the configured default page, or the workspace root."""
        url = _DEFAULT_URL
        if self._settings and self._settings.default_panel_page:
            url = self._settings.default_panel_page
        self.open(url)

    def close(self):
        """Remove the Fibery panel and restore the original window size."""
        if sys.platform != "win32":
            return
        if self._split is None:
            return

        form = _get_winforms_form(self._main)
        from System import Func, Type
        form.Invoke(Func[Type](lambda: self._destroy_panel(form)))

    # ------------------------------------------------------------------
    # Internal - UI thread helpers
    # ------------------------------------------------------------------

    def _build_panel(self, form, url: str):
        """Create SplitContainer + second WebView2. Runs on UI thread."""
        try:
            import System.Windows.Forms as WinForms
            import System.Drawing as Drawing

            # Grab the existing WebView2 control (stored directly on form by pywebview)
            wv = form.webview

            # Record original geometry
            self._original_width = form.Width
            self._original_min_width = form.MinimumSize.Width
            chrome_w = form.Width - form.ClientSize.Width

            # Compute left-pane width from the pywebview min_size (logical px).
            # form.MinimumSize is unreliable (WinForms DPI auto-scale can
            # make it equal to form.Width). So we read the logical value and
            # scale it ourselves using the ratio of physical/logical width.
            min_logical_w = self._main.min_size[0]
            initial_logical_w = self._main.initial_width
            if min_logical_w and min_logical_w > 0:
                dpi_scale = self._original_min_width / min_logical_w
            else:
                dpi_scale = form.DeviceDpi / 96.0
            splitter_dist = int(min_logical_w * dpi_scale) - chrome_w

            logger.info(
                "DPI debug: min_logical=%d, initial_logical=%d, form.Width=%d, "
                "form.MinimumSize.Width=%d, DeviceDpi=%d, dpi_scale=%.4f, "
                "chrome_w=%d, splitter_dist=%d",
                min_logical_w, initial_logical_w or 0, self._original_width,
                self._original_min_width, form.DeviceDpi, dpi_scale,
                chrome_w, splitter_dist,
            )

            # Create the second WebView2 BEFORE modifying the form layout,
            # so a failure here leaves the window untouched.
            panel_wv = self._create_webview2(url)
            panel_wv.Dock = WinForms.DockStyle.Fill

            # --- Build SplitContainer ---
            split = WinForms.SplitContainer()
            split.Dock = WinForms.DockStyle.Fill
            split.Orientation = WinForms.Orientation.Vertical
            split.FixedPanel = WinForms.FixedPanel.Panel1
            split.IsSplitterFixed = True
            split.SplitterWidth = 1

            # Move existing WebView2 into Panel1
            form.Controls.Remove(wv)
            wv.Dock = WinForms.DockStyle.Fill
            split.Panel1.Controls.Add(wv)
            split.Panel2.Controls.Add(panel_wv)

            # Add SplitContainer to form so it gets a real size
            form.Controls.Add(split)
            form.Controls.SetChildIndex(split, 0)

            # Expand window BEFORE setting splitter distance
            # (SplitContainer validates these against its current Width)
            new_min_w = splitter_dist + _MIN_PANEL_WIDTH + chrome_w + split.SplitterWidth
            form.MinimumSize = Drawing.Size(
                new_min_w, form.MinimumSize.Height
            )
            form.Width = max(self._original_width + _PANEL_DEFAULT_WIDTH, form.MinimumSize.Width)

            # Now the SplitContainer has a real Width - safe to set constraints.
            # Keep Panel1MinSize low so it doesn't clamp SplitterDistance upward
            # (splitter is fixed anyway so the user can't drag it).
            split.Panel1MinSize = splitter_dist
            split.Panel2MinSize = _MIN_PANEL_WIDTH
            split.SplitterDistance = splitter_dist

            self._split = split
            self._panel_wv = panel_wv

            logger.info(
                "Entity panel opened - original_w: %d, client_w: %d, "
                "new_min: %d, new_w: %d, splitter_dist_set: %d, "
                "panel1_actual: %d, panel2_actual: %d, split_w: %d",
                self._original_width, form.ClientSize.Width,
                form.MinimumSize.Width, form.Width, split.SplitterDistance,
                split.Panel1.Width, split.Panel2.Width, split.Width
            )

        except Exception:
            logger.exception("Failed to build entity panel")

    def _create_webview2(self, url: str):
        """Create and return a new WebView2 control with persistent storage.

        NOTE: CoreWebView2CreationProperties is in WinForms namespace, not Core.
        """
        # These are already loaded by edgechromium.py - just import them
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2

        panel_wv = WebView2()

        # Persistent user-data folder so Fibery login survives restarts
        props = CoreWebView2CreationProperties()
        cache = _get_cache_dir()
        if cache:
            props.UserDataFolder = os.path.join(cache, "fibery_panel")
        panel_wv.CreationProperties = props

        # Navigate once the core is ready
        _url = url

        def on_init_complete(sender, args):
            try:
                if args.IsSuccess:
                    sender.CoreWebView2.Navigate(_url)
                else:
                    logger.error("WebView2 init failed: %s", args.InitializationException)
            except Exception:
                logger.exception("Navigation after init failed")

        panel_wv.CoreWebView2InitializationCompleted += on_init_complete

        # Track URL changes (SPA pushState) and notify JS
        main_window = self._main
        def on_source_changed(sender, args):
            try:
                url = sender.CoreWebView2.Source if sender.CoreWebView2 else ""
                if main_window:
                    import json
                    import threading
                    # Fire on background thread to avoid UI-thread deadlock with evaluate_js
                    threading.Thread(
                        target=lambda: main_window.evaluate_js(
                            f"window.onPanelUrlChanged && window.onPanelUrlChanged({json.dumps(url)})"
                        ),
                        daemon=True,
                    ).start()
            except Exception:
                logger.debug("SourceChanged callback error", exc_info=True)

        panel_wv.SourceChanged += on_source_changed
        panel_wv.EnsureCoreWebView2Async(None)
        return panel_wv

    def _navigate(self, url: str):
        """Navigate the already-open panel to a new URL."""
        if self._panel_wv is None:
            return
        try:
            form = _get_winforms_form(self._main)
            from System import Func, Type
            panel_wv = self._panel_wv

            def _do():
                try:
                    if panel_wv.CoreWebView2 is not None:
                        panel_wv.CoreWebView2.Navigate(url)
                except Exception:
                    logger.exception("Navigate failed")

            form.Invoke(Func[Type](_do))
        except Exception:
            logger.exception("_navigate failed")

    def _destroy_panel(self, form):
        """Remove SplitContainer and restore original layout. Runs on UI thread."""
        try:
            import System.Windows.Forms as WinForms
            import System.Drawing as Drawing

            split = self._split
            panel_wv = self._panel_wv

            if split is None:
                return

            # Recover the main WebView2 from Panel1
            wv = None
            if split.Panel1.Controls.Count > 0:
                wv = split.Panel1.Controls[0]
                split.Panel1.Controls.Remove(wv)

            form.Controls.Remove(split)

            # Restore the main WebView2 to the form
            if wv is not None:
                wv.Dock = WinForms.DockStyle.Fill
                form.Controls.Add(wv)
                form.Controls.SetChildIndex(wv, 0)

            # Dispose panel WebView2 and SplitContainer
            try:
                if panel_wv is not None:
                    panel_wv.Dispose()
            except Exception:
                logger.debug("panel_wv dispose error", exc_info=True)

            try:
                split.Dispose()
            except Exception:
                logger.debug("split dispose error", exc_info=True)

            # Restore original window size (min-size first to avoid clamping)
            if self._original_min_width is not None:
                form.MinimumSize = Drawing.Size(
                    self._original_min_width, form.MinimumSize.Height
                )
            if self._original_width is not None:
                form.Width = self._original_width

            self._split = None
            self._panel_wv = None

            logger.info("Entity panel closed")

        except Exception:
            logger.exception("Failed to destroy entity panel")
