"""AI 模型设置对话框 — 只包含 AI 提供商相关字段."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.settings import Settings, save_settings
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.ai.cursor_connector import (
    is_openclaw_cs_model,
    should_use_cursor_provider,
)
from pa_agent.ai.qclaw_connector import (
    detect_qclaw,
    is_openclaw_model,
    should_use_qclaw_provider,
)
from pa_agent.ai.workbuddy_connector import (
    detect_workbuddy,
    is_openclaw_wb_model,
    should_use_workbuddy_provider,
)




class AIModelSettingsDialog(QDialog):
    """AI 模型 / 提供商配置对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 模型设置")
        self.setMinimumWidth(520)
        self._settings = settings
        self._setup_ui()
        self._load_values()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        provider_group = QGroupBox("AI 提供商")
        form = QFormLayout(provider_group)

        self._model_edit = QLineEdit()
        form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        form.addRow("Base URL:", self._base_url_edit)

        api_key_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self._api_key_edit)
        self._show_key_btn = QPushButton("隐藏")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(52)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_key_row.addWidget(self._show_key_btn)
        form.addRow("API Key:", api_key_row)

        self._thinking_check = QCheckBox("启用 Thinking")
        form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        root.addWidget(provider_group)

        # ── 网络代理（仅作用于 AI 客户端；行情始终直连） ──────────────────────
        proxy_group = QGroupBox("网络代理")
        proxy_form = QFormLayout(proxy_group)

        self._proxy_enabled_check = QCheckBox("启用代理（仅对 AI 请求生效）")
        self._proxy_enabled_check.setToolTip(
            "启用后，AI 大模型请求将通过下方代理转发（用于访问 DeepSeek/OpenAI 等国外网关）。\n"
            "行情数据源（A股/东方财富）始终直连，不受此设置影响。"
        )
        self._proxy_enabled_check.toggled.connect(self._on_proxy_enabled_toggled)
        proxy_form.addRow(self._proxy_enabled_check)

        self._proxy_scheme_combo = QComboBox()
        self._proxy_scheme_combo.addItem("HTTP", "http")
        self._proxy_scheme_combo.addItem("SOCKS5", "socks5")
        proxy_form.addRow("协议:", self._proxy_scheme_combo)

        self._proxy_host_edit = QLineEdit()
        self._proxy_host_edit.setPlaceholderText("127.0.0.1")
        proxy_form.addRow("代理地址:", self._proxy_host_edit)

        self._proxy_port_spin = QSpinBox()
        self._proxy_port_spin.setRange(1, 65535)
        self._proxy_port_spin.setValue(7890)
        proxy_form.addRow("端口:", self._proxy_port_spin)

        root.addWidget(proxy_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setText("保存")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── 加载 / 保存 ────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        p = self._settings.provider
        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)
        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)
        self._proxy_enabled_check.blockSignals(True)
        self._proxy_enabled_check.setChecked(bool(getattr(p, "proxy_enabled", False)))
        self._proxy_enabled_check.blockSignals(False)
        scheme = getattr(p, "proxy_scheme", "http")
        sidx = self._proxy_scheme_combo.findData(scheme)
        if sidx >= 0:
            self._proxy_scheme_combo.setCurrentIndex(sidx)
        self._proxy_host_edit.setText(getattr(p, "proxy_host", "127.0.0.1"))
        self._proxy_port_spin.setValue(int(getattr(p, "proxy_port", 7890)))
        self._on_proxy_enabled_toggled(self._proxy_enabled_check.isChecked())

    def _on_save(self) -> None:
        p = self._settings.provider
        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        api_key = self._api_key_edit.text().strip()

        # Explicit model aliases win over stale base_url (openclaw_wb before openclaw).
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_workbuddy_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "WorkBuddy 配置异常", err)
                return
        elif is_openclaw_cs_model(model) or should_use_cursor_provider(model, base_url):
            # Cursor route must keep the user-provided Cursor API key (crsr_...).
            p.api_key = api_key
            err = self._apply_cursor_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "Cursor 配置异常", err)
                return
        elif is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_qclaw_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "QClaw 配置异常", err)
                return
        else:
            field_err = self._validate_provider_fields(model, base_url)
            if field_err:
                QMessageBox.warning(self, "AI 提供商配置有误", field_err)
                return
            p.model = model
            p.base_url = base_url
            p.api_key = api_key

        p.thinking = self._thinking_check.isChecked()
        p.reasoning_effort = self._reasoning_effort_combo.currentText()  # type: ignore[assignment]

        p.proxy_enabled = self._proxy_enabled_check.isChecked()
        p.proxy_scheme = self._proxy_scheme_combo.currentData()  # type: ignore[assignment]
        p.proxy_host = self._proxy_host_edit.text().strip() or "127.0.0.1"
        p.proxy_port = self._proxy_port_spin.value()

        save_settings(self._settings, SETTINGS_JSON_PATH)
        self.accept()

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def focus_api_key_field(self) -> None:
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("显示")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("隐藏")

    def _on_proxy_enabled_toggled(self, enabled: bool) -> None:
        """启用代理时才允许编辑地址/端口；未启用时置灰。"""
        for w in (self._proxy_scheme_combo, self._proxy_host_edit, self._proxy_port_spin):
            w.setEnabled(enabled)

    def _apply_cursor_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.cursor_connector import apply_cursor_provider_to_settings
        return apply_cursor_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_qclaw_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.qclaw_connector import apply_qclaw_provider_to_settings
        return apply_qclaw_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_workbuddy_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.workbuddy_connector import apply_workbuddy_provider_to_settings
        return apply_workbuddy_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    @staticmethod
    def _validate_provider_fields(model: str, base_url: str) -> str | None:
        if is_openclaw_cs_model(model) or should_use_cursor_provider(model, base_url):
            return None
        if is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            return None
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            return None
        if model.startswith(("http://", "https://")) and not base_url.startswith(("http://", "https://")):
            return (
                "「模型」与「Base URL」似乎填反了：\n"
                "• 模型应填模型名，如 deepseek-v4-pro 或 claude-sonnet-4-6\n"
                "• 使用 QClaw 时模型填 openclaw（或 openclaw/main）\n"
                "• 使用 Cursor 订阅时模型填 openclaw_cs\n"
                "• 使用 WorkBuddy 时模型填 openclaw_wb\n"
                "• Base URL 应填接口地址，如 https://api.deepseek.com"
            )
        if base_url.startswith(("http://", "https://")):
            return None
        if not base_url:
            if detect_qclaw():
                return (
                    "请填写 Base URL，或使用 QClaw/WorkBuddy：\n"
                    "• 模型填 openclaw → QClaw\n"
                    "• 模型填 openclaw_cs → Cursor 订阅（经 QClaw 网关）\n"
                    "• 模型填 openclaw_wb → WorkBuddy"
                )
            if detect_workbuddy():
                return "请填写 Base URL，或使用 WorkBuddy：\n• 模型填 openclaw_wb（保存时自动配置）"
            return "请填写 Base URL（API 接口地址）。"
        return (
            f"Base URL 不是有效网址（当前：{base_url}）。\n"
            "DeepSeek 示例：https://api.deepseek.com\n"
            "PackyAPI 示例：https://www.packyapi.com/v1\n"
            "QClaw：模型填 openclaw 后点保存（自动配置本地网关）\n"
            "Cursor：模型填 openclaw_cs 后点保存（经 QClaw 走 Cursor 订阅）\n"
            "WorkBuddy：模型填 openclaw_wb 后点保存（自动配置 WorkBuddy）"
        )
