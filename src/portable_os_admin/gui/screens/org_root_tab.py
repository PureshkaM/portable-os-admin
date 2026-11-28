"""Вкладка «Корень организации» (LIVE-2.0, Э18).

Что здесь делает администратор:

* создаёт резервный корень -- файл и сгенерированную фразу; фраза
  показывается один раз, окно не закрывается, пока не отмечено «записал»;
* выгружает запрос сертификата своего рабочего ключа и ставит полученный
  сертификат;
* как владелец корня -- выпускает сертификат по чужому запросу или сразу
  этому контейнеру.

Основной корень (Рутокен ЭЦП 3.0) появится здесь вместе с поддержкой токена.
Вся криптография и её обоснование -- в `admin/org_root.py`.
"""
from __future__ import annotations

import datetime
import json
from ..ui import tk
from pathlib import Path
from ..ui import filedialog, ttk

from ...admin import org_root
from ...install.access.runtime import poa_trust
from .. import theme
from ..widgets import facts_table, run_in_background

ROLE_NAMES = {"main": "основной", "reserve": "резервный"}


class OrgRootTab:
    def __init__(self, page: ttk.Frame, frame) -> None:
        self.page = page
        self.frame = frame
        self.app = frame.app
        self.bar = frame.message_bar
        self.facts = ttk.Frame(page, style="Card.TFrame")
        self.facts.pack(fill="x", anchor="w")
        row = ttk.Frame(page, style="Card.TFrame")
        row.pack(anchor="w", pady=(theme.SPACE_SM, 0))
        self.create_button = ttk.Button(row, text="Создать резервный корень…", command=self._create)
        self.create_button.pack(side="left")
        ttk.Button(row, text="Сертификат этому контейнеру…", command=self._certify_self).pack(
            side="left", padx=theme.SPACE_XS)
        ttk.Button(row, text="Выпустить по запросу…", command=self._certify_request).pack(side="left")
        row2 = ttk.Frame(page, style="Card.TFrame")
        row2.pack(anchor="w", pady=(theme.SPACE_XS, 0))
        ttk.Button(row2, text="Запрос сертификата…", command=self._export_request).pack(side="left")
        ttk.Button(row2, text="Установить сертификат…", command=self._install).pack(
            side="left", padx=theme.SPACE_XS)
        self.refresh()

    @property
    def container(self):
        return self.app.session.container

    def refresh(self) -> None:
        for child in self.facts.winfo_children():
            child.destroy()
        anchors = org_root.anchors(self.container)
        rows = [(f"Корень, {ROLE_NAMES.get(a.get('role'), a.get('role', ''))}", org_root.fingerprint(a))
                for a in anchors] or [("Корни", "не заданы")]
        rows.append(("Сертификат", self._certificate_text(anchors)))
        facts_table(self.facts, tuple(rows))
        has_reserve = any(a.get("role") == "reserve" for a in anchors)
        self.create_button.state(["disabled"] if has_reserve else ["!disabled"])

    def _certificate_text(self, anchors) -> str:
        raw = self.container.data.working_key_cert_json
        if not raw:
            return "нет"
        cert = json.loads(raw)
        try:
            poa_trust.accepted_key(anchors, cert)
        except poa_trust.TrustError as exc:
            return f"недействителен: {exc}"
        until = datetime.date.fromtimestamp(cert["cert"]["not_after"]).strftime("%d.%m.%Y")
        return f"действует до {until}"

    # -- действия ---------------------------------------------------------
    def _create(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.page, title="Файл резервного корня",
                                            defaultextension=".poaroot",
                                            filetypes=[("Корень организации", "*.poaroot")])
        if not path:
            return
        self.bar.info("Создаю корень…")

        def done(result) -> None:
            phrase, entry = result
            anchors = [a for a in org_root.anchors(self.container) if a.get("role") != "reserve"]
            org_root.set_anchors(self.container, anchors + [entry])
            self.app.save_container()
            self.refresh()
            PhraseDialog(self.page, phrase, entry)
            self.bar.success("Резервный корень создан.")

        run_in_background(self.page, lambda: org_root.create_reserve_root(Path(path)), done,
                          lambda exc: self.bar.error(f"Корень не создан: {exc}"))

    def _open_root(self, then) -> None:
        path = filedialog.askopenfilename(parent=self.page, title="Файл корня",
                                          filetypes=[("Корень организации", "*.poaroot")])
        if not path:
            return
        PhraseEntryDialog(self.page, lambda phrase: self._unlock(Path(path), phrase, then))

    def _unlock(self, path: Path, phrase: str, then) -> None:
        self.bar.info("Открываю корень…")
        run_in_background(self.page, lambda: org_root.open_reserve_root(path, phrase), then,
                          lambda exc: self.bar.error(str(exc)))

    def _certify_self(self) -> None:
        def certify(root) -> None:
            try:
                org_root.certify_container(root, self.container)
            except (org_root.OrgRootError, poa_trust.TrustError) as exc:
                self.bar.error(f"Сертификат не выдан: {exc}")
                return
            self.app.save_container()
            self.refresh()
            self.bar.success("Сертификат выдан этому контейнеру.")
        self._open_root(certify)

    def _certify_request(self) -> None:
        source = filedialog.askopenfilename(parent=self.page, title="Запрос сертификата",
                                            filetypes=[("Запрос", "*.poareq")])
        if not source:
            return
        request = json.loads(Path(source).read_text(encoding="utf-8"))
        key_fp = poa_trust.key_id({"alg": poa_trust.ALG_ED25519, "key": request.get("key", "")})
        if not ConfirmDialog.ask(self.page, "Выпустить сертификат",
                                 (("Администратор", request.get("name", "")),
                                  ("Отпечаток ключа", " ".join(key_fp[i:i + 4] for i in range(0, 16, 4))))):
            return

        def certify(root) -> None:
            certificate = org_root.certify_request(root, request)
            bundle = {"certificate": certificate, "anchors": org_root.anchors(self.container)}
            target = filedialog.asksaveasfilename(parent=self.page, title="Сохранить сертификат",
                                                  defaultextension=".poacert",
                                                  filetypes=[("Сертификат", "*.poacert")])
            if target:
                Path(target).write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
                self.bar.success("Сертификат сохранён.")
        self._open_root(certify)

    def _export_request(self) -> None:
        target = filedialog.asksaveasfilename(parent=self.page, title="Запрос сертификата",
                                              defaultextension=".poareq",
                                              filetypes=[("Запрос", "*.poareq")])
        if not target:
            return
        request = org_root.admin_request(self.container, "")
        Path(target).write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        key_fp = poa_trust.key_id({"alg": poa_trust.ALG_ED25519, "key": request["key"]})
        self.bar.success("Запрос сохранён. Отпечаток ключа для сверки: "
                         + " ".join(key_fp[i:i + 4] for i in range(0, 16, 4)))

    def _install(self) -> None:
        source = filedialog.askopenfilename(parent=self.page, title="Сертификат",
                                            filetypes=[("Сертификат", "*.poacert")])
        if not source:
            return
        bundle = json.loads(Path(source).read_text(encoding="utf-8"))
        known = org_root.anchors(self.container)
        if not known:
            # Первые якоря контейнер узнаёт из файла -- поэтому их отпечатки
            # сверяются глазами с бумагой, выданной при создании корней.
            rows = tuple((f"Корень, {ROLE_NAMES.get(a.get('role'), '')}", org_root.fingerprint(a))
                         for a in bundle.get("anchors", []))
            if not rows or not ConfirmDialog.ask(self.page, "Корни организации", rows):
                return
            org_root.set_anchors(self.container, bundle["anchors"])
        try:
            org_root.install_certificate(self.container, bundle["certificate"])
        except (org_root.OrgRootError, poa_trust.TrustError) as exc:
            self.bar.error(f"Сертификат не установлен: {exc}")
            return
        self.app.save_container()
        self.refresh()
        self.bar.success("Сертификат установлен.")


class PhraseDialog(tk.Toplevel):
    """Фраза резервного корня -- один раз. Закрыть -- только отметив «записал»."""

    def __init__(self, parent, phrase: str, entry: dict) -> None:
        super().__init__(parent)
        self.title("Фраза резервного корня")
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        body = ttk.Frame(self, padding=theme.SPACE_MD)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Фраза показывается один раз. Храните её отдельно от файла корня.",
                  wraplength=theme.TEXT_WRAP).pack(anchor="w")
        field = ttk.Entry(body, font=theme.FONT_MONO, width=44, justify="center")
        field.insert(0, org_root.format_phrase(phrase))
        field.state(["readonly"])
        field.pack(fill="x", pady=theme.SPACE_SM)
        facts_table(body, (("Отпечаток корня", org_root.fingerprint(entry)),))
        self.written = tk.BooleanVar(value=False)
        done = ttk.Button(body, text="Готово", command=self.destroy, state="disabled")
        ttk.Checkbutton(body, text="Фраза записана", variable=self.written,
                        command=lambda: done.state(["!disabled"] if self.written.get() else ["disabled"])
                        ).pack(anchor="w", pady=(theme.SPACE_SM, 0))
        done.pack(anchor="e", pady=(theme.SPACE_SM, 0))
        self.grab_set()


class PhraseEntryDialog(tk.Toplevel):
    def __init__(self, parent, on_accept) -> None:
        super().__init__(parent)
        self.title("Фраза корня")
        self.transient(parent)
        self.on_accept = on_accept
        body = ttk.Frame(self, padding=theme.SPACE_MD)
        body.pack(fill="both", expand=True)
        self.phrase = tk.StringVar()
        entry = ttk.Entry(body, textvariable=self.phrase, show="•", width=44, font=theme.FONT_MONO)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _e: self._ok())
        ttk.Button(body, text="Открыть", command=self._ok).pack(anchor="e", pady=(theme.SPACE_SM, 0))
        entry.focus_set()
        self.grab_set()

    def _ok(self) -> None:
        phrase = self.phrase.get()
        self.destroy()
        if phrase:
            self.on_accept(phrase)


class ConfirmDialog(tk.Toplevel):
    """Сверка отпечатков перед подписью или доверием -- «да» или «нет»."""

    def __init__(self, parent, title: str, rows) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.answer = False
        body = ttk.Frame(self, padding=theme.SPACE_MD)
        body.pack(fill="both", expand=True)
        facts_table(body, tuple(rows))
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(theme.SPACE_SM, 0))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left")
        ttk.Button(buttons, text="Отпечатки совпадают", command=self._yes).pack(
            side="left", padx=(theme.SPACE_XS, 0))
        self.grab_set()

    def _yes(self) -> None:
        self.answer = True
        self.destroy()

    @classmethod
    def ask(cls, parent, title: str, rows) -> bool:
        dialog = cls(parent, title, rows)
        parent.wait_window(dialog)
        return dialog.answer


def build_org_root_tab(notebook: ttk.Notebook, frame) -> OrgRootTab:
    page = ttk.Frame(notebook, style="Card.TFrame", padding=theme.SPACE_MD)
    notebook.add(page, text="Корень")
    return OrgRootTab(page, frame)
