from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BrowserUseModelField:
    name: str
    annotation: str
    required: bool
    default: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserUseRegisteredAction:
    name: str
    description: str
    param_model: str | None
    terminates_sequence: bool
    source_line: int


@dataclass(frozen=True, slots=True)
class BrowserActionDescriptor:
    action: str
    source_action: str
    source_model: str | None
    description: str
    implemented: bool
    zyra_required_arguments: tuple[str, ...]
    zyra_optional_arguments: tuple[str, ...]
    source_required_fields: tuple[str, ...]
    source_optional_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserPlanValidationIssue:
    step_index: int
    action: str
    reason: str


class BrowserActionRegistry:
    def __init__(
        self,
        *,
        descriptors: list[BrowserActionDescriptor],
        aliases: dict[str, str],
        source_actions: list[BrowserUseRegisteredAction],
        source_models_path: Path,
        source_service_path: Path,
    ) -> None:
        self._descriptors = {descriptor.action: descriptor for descriptor in descriptors}
        self._aliases = aliases
        self.source_actions = source_actions
        self.source_models_path = source_models_path
        self.source_service_path = source_service_path

    def get(self, action: str) -> BrowserActionDescriptor | None:
        return self._descriptors.get(self.normalize_action(action))

    def normalize_action(self, action: str) -> str:
        return self._aliases.get(action, action)

    def validate_plan(self, plan: list[dict[str, Any]]) -> list[BrowserPlanValidationIssue]:
        issues: list[BrowserPlanValidationIssue] = []
        for index, step in enumerate(plan, start=1):
            action = str(step.get("action") or step.get("browser_action") or "")
            descriptor = self.get(action)
            if descriptor is None:
                issues.append(BrowserPlanValidationIssue(index, action, "unknown_browser_action"))
                continue
            arguments = step.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            for argument_name in descriptor.zyra_required_arguments:
                if arguments.get(argument_name) in (None, ""):
                    issues.append(
                        BrowserPlanValidationIssue(
                            index,
                            action,
                            f"missing_required_argument:{argument_name}",
                        )
                    )
        return issues

    def describe(self) -> dict[str, Any]:
        return {
            "source": "browser-use",
            "source_models_path": str(self.source_models_path),
            "source_service_path": str(self.source_service_path),
            "actions": [asdict(item) for item in self._descriptors.values()],
            "aliases": dict(self._aliases),
            "source_registered_actions": [asdict(item) for item in self.source_actions],
        }


def default_browser_action_registry(project_root: str | Path) -> BrowserActionRegistry:
    root = Path(project_root)
    models_path = root / "vendor" / "browser-use" / "browser_use" / "tools" / "views.py"
    service_path = root / "vendor" / "browser-use" / "browser_use" / "tools" / "service.py"
    model_fields = load_browser_use_action_models(models_path)
    source_actions = load_browser_use_registered_actions(service_path)
    source_by_name = {action.name: action for action in source_actions}

    descriptors = [
        _descriptor(
            action="open_url",
            source_action="navigate",
            source_model="NavigateAction",
            fallback_description="Navigate to a URL.",
            zyra_required_arguments=("url",),
            zyra_optional_arguments=("new_tab",),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="extract_text",
            source_action="extract",
            source_model="ExtractAction",
            fallback_description="Extract visible page text and links into artifacts.",
            zyra_required_arguments=(),
            zyra_optional_arguments=(
                "query",
                "extract_links",
                "extract_images",
                "start_from_char",
                "already_collected",
            ),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="snapshot_state",
            source_action="find_elements",
            source_model="FindElementsAction",
            fallback_description="Capture a structured browser state summary.",
            zyra_required_arguments=(),
            zyra_optional_arguments=("selector", "attributes", "max_results", "include_text"),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="click_element",
            source_action="click",
            source_model="ClickElementAction",
            fallback_description="Click an indexed element in the current browser state.",
            zyra_required_arguments=("index",),
            zyra_optional_arguments=("coordinate_x", "coordinate_y"),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="input_text",
            source_action="input",
            source_model="InputTextAction",
            fallback_description="Input text into an indexed element.",
            zyra_required_arguments=("index", "text"),
            zyra_optional_arguments=("clear",),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="upload_file",
            source_action="upload_file",
            source_model="UploadFileAction",
            fallback_description="Upload a workspace file through a file input resolved from index, id, or #id selector.",
            zyra_required_arguments=("path",),
            zyra_optional_arguments=("index", "id", "element_id", "selector"),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="search_page",
            source_action="search_page",
            source_model="SearchPageAction",
            fallback_description="Search current page text for a literal or regex pattern.",
            zyra_required_arguments=("pattern",),
            zyra_optional_arguments=("regex", "case_sensitive", "context_chars", "css_scope", "max_results"),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="collect_downloads",
            source_action="downloaded_files",
            source_model=None,
            fallback_description="Collect files downloaded by the browser-use session into Zyra artifacts.",
            zyra_required_arguments=(),
            zyra_optional_arguments=("settle_seconds",),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="wait",
            source_action="wait",
            source_model=None,
            fallback_description="Wait for the current page or browser event loop to settle.",
            zyra_required_arguments=(),
            zyra_optional_arguments=("seconds",),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="go_back",
            source_action="go_back",
            source_model="NoParamsAction",
            fallback_description="Navigate back in the current browser tab.",
            zyra_required_arguments=(),
            zyra_optional_arguments=("description",),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="scroll_page",
            source_action="scroll",
            source_model="ScrollAction",
            fallback_description="Scroll the page or a scrollable indexed element.",
            zyra_required_arguments=(),
            zyra_optional_arguments=("down", "pages", "index"),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="send_keys",
            source_action="send_keys",
            source_model="SendKeysAction",
            fallback_description="Send keyboard input or shortcuts to the browser.",
            zyra_required_arguments=("keys",),
            zyra_optional_arguments=(),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="scroll_to_text",
            source_action="find_text",
            source_model=None,
            fallback_description="Scroll to visible text on the current page.",
            zyra_required_arguments=("text",),
            zyra_optional_arguments=(),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="take_screenshot",
            source_action="screenshot",
            source_model="ScreenshotAction",
            fallback_description="Capture the current browser viewport or full page as a screenshot artifact.",
            zyra_required_arguments=(),
            zyra_optional_arguments=("file_name", "full_page", "format", "quality"),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="save_as_pdf",
            source_action="save_as_pdf",
            source_model="SaveAsPdfAction",
            fallback_description="Capture the current page as a PDF artifact.",
            zyra_required_arguments=(),
            zyra_optional_arguments=(
                "file_name",
                "print_background",
                "landscape",
                "scale",
                "paper_format",
                "display_header_footer",
            ),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
        _descriptor(
            action="evaluate_js",
            source_action="evaluate",
            source_model=None,
            fallback_description="Execute browser JavaScript through the browser-use CDP runtime.",
            zyra_required_arguments=("code",),
            zyra_optional_arguments=(),
            source_by_name=source_by_name,
            model_fields=model_fields,
        ),
    ]
    return BrowserActionRegistry(
        descriptors=descriptors,
        aliases={
            "navigate": "open_url",
            "extract": "extract_text",
            "find_elements": "snapshot_state",
            "click": "click_element",
            "input": "input_text",
            "upload": "upload_file",
            "downloads": "collect_downloads",
            "collect_download": "collect_downloads",
            "scroll": "scroll_page",
            "find_text": "scroll_to_text",
            "screenshot": "take_screenshot",
            "pdf": "save_as_pdf",
            "evaluate": "evaluate_js",
            "javascript": "evaluate_js",
        },
        source_actions=source_actions,
        source_models_path=models_path,
        source_service_path=service_path,
    )


def load_browser_use_action_models(path: str | Path) -> dict[str, tuple[BrowserUseModelField, ...]]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    models: dict[str, tuple[BrowserUseModelField, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Action"):
            continue
        fields: list[BrowserUseModelField] = []
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            if statement.target.id == "model_config":
                continue
            fields.append(_model_field_from_ast(statement))
        models[node.name] = tuple(fields)
    return models


def load_browser_use_registered_actions(path: str | Path) -> list[BrowserUseRegisteredAction]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    actions: list[BrowserUseRegisteredAction] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        decorator = next((_action_decorator(item) for item in node.decorator_list if _action_decorator(item)), None)
        if decorator is None:
            continue
        actions.append(
            BrowserUseRegisteredAction(
                name=node.name,
                description=_decorator_description(decorator),
                param_model=_decorator_param_model(decorator) or _first_param_model(node),
                terminates_sequence=_decorator_bool(decorator, "terminates_sequence"),
                source_line=node.lineno,
            )
        )
    return sorted(actions, key=lambda item: item.source_line)


def _descriptor(
    *,
    action: str,
    source_action: str,
    source_model: str | None,
    fallback_description: str,
    zyra_required_arguments: tuple[str, ...],
    zyra_optional_arguments: tuple[str, ...],
    source_by_name: dict[str, BrowserUseRegisteredAction],
    model_fields: dict[str, tuple[BrowserUseModelField, ...]],
) -> BrowserActionDescriptor:
    fields = model_fields.get(source_model or "", ())
    source = source_by_name.get(source_action)
    return BrowserActionDescriptor(
        action=action,
        source_action=source_action,
        source_model=source_model,
        description=(source.description if source and source.description else fallback_description),
        implemented=True,
        zyra_required_arguments=zyra_required_arguments,
        zyra_optional_arguments=zyra_optional_arguments,
        source_required_fields=tuple(field.name for field in fields if field.required),
        source_optional_fields=tuple(field.name for field in fields if not field.required),
    )


def _model_field_from_ast(statement: ast.AnnAssign) -> BrowserUseModelField:
    value = statement.value
    description = None
    default = None
    required = value is None
    if isinstance(value, ast.Call) and _call_name(value.func) == "Field":
        description = _keyword_string(value, "description")
        default = _field_default(value)
        required = default is None and not _has_keyword(value, "default_factory")
    elif value is not None:
        default = ast.unparse(value)
        required = False
    return BrowserUseModelField(
        name=statement.target.id,
        annotation=ast.unparse(statement.annotation),
        required=required,
        default=default,
        description=description,
    )


def _action_decorator(decorator: ast.expr) -> ast.Call | None:
    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
        return decorator if decorator.func.attr == "action" else None
    return None


def _decorator_description(decorator: ast.Call) -> str:
    if not decorator.args:
        return ""
    value = decorator.args[0]
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else ""


def _decorator_param_model(decorator: ast.Call) -> str | None:
    for keyword in decorator.keywords:
        if keyword.arg == "param_model":
            return _name_from_expr(keyword.value)
    return None


def _decorator_bool(decorator: ast.Call, name: str) -> bool:
    for keyword in decorator.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value is True
    return False


def _first_param_model(node: ast.AsyncFunctionDef | ast.FunctionDef) -> str | None:
    for arg in node.args.args:
        if arg.annotation is not None:
            name = _name_from_expr(arg.annotation)
            if name and name.endswith("Action"):
                return name
    return None


def _name_from_expr(value: ast.expr) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        return _name_from_expr(value.left)
    return None


def _keyword_string(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _field_default(call: ast.Call) -> str | None:
    if call.args:
        return ast.unparse(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == "default":
            return ast.unparse(keyword.value)
    return None


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


def _call_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""
