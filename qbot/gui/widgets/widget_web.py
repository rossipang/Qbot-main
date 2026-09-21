from pathlib import Path
import shutil
import time

import wx
import wx.html2 as web


# WebView2 NavigateToString（SetPage）对超大字符串会 E_INVALIDARG（约 1～2MB）
_SETPAGE_MAX_BYTES = 800_000


class WebPanel(wx.Panel):
    def __init__(self, parent, id=-1):
        super(WebPanel, self).__init__(parent, id)

        vbox = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(vbox)
        self.browser = web.WebView.New(self)
        vbox.Add(self.browser, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

    def show_url(self, url):
        self.browser.LoadURL(url)
        self.browser.Show()
        self.Layout()

    def show_file(self, filename):
        """
        用 file:// 加载本地 HTML。

        注意：Windows WebView2 对 ``file:///...?t=时间戳`` 经常直接空白，
        不能用 query 破缓存；改为旁路拷贝一份带时间戳的文件名再 LoadURL。
        超大 INLINE 页不要走 SetPage，否则 NavigateToString 直接失败变空白。
        """
        path = Path(filename).resolve()
        if not path.exists():
            raise FileNotFoundError(f"HTML 文件不存在: {path}")

        load_path = path
        try:
            stamp = int(time.time() * 1000)
            load_path = path.with_name(f"{path.stem}__v{stamp}{path.suffix}")
            shutil.copy2(path, load_path)
            self._cleanup_stale_copies(path, keep=3)
        except Exception:
            load_path = path

        uri = load_path.as_uri()
        size = 0
        try:
            size = path.stat().st_size
        except Exception:
            size = 0

        try:
            self.browser.LoadURL(uri)
        except Exception:
            if size and size <= _SETPAGE_MAX_BYTES:
                html_cont = path.read_text(encoding="utf-8", errors="ignore")
                self.browser.SetPage(html_cont, path.as_uri())
            else:
                raise

        self.browser.Show()
        self.Layout()
        self.Refresh()

    @staticmethod
    def _cleanup_stale_copies(base: Path, keep: int = 3) -> None:
        """清理同名前缀的旧破缓存副本，避免 bkt_result 堆积。"""
        try:
            copies = sorted(
                base.parent.glob(f"{base.stem}__v*{base.suffix}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in copies[keep:]:
                try:
                    if old.exists():
                        old.unlink()
                except Exception:
                    pass
        except Exception:
            pass
