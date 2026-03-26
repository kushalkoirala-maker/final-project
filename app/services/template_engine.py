from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound


class TemplateEngineError(Exception):
    pass


class TemplateNotAvailableError(TemplateEngineError):
    pass


class TemplateRenderError(TemplateEngineError):
    pass


TEMPLATE_MAP = {
    "vlan_creation": "vlan_creation.j2",
    "access_port_assignment": "access_port_assignment.j2",
    "trunk_setup": "trunk_setup.j2",
    "static_route": "static_route.j2",
}

TEMPLATE_SCHEMA = {
    "vlan_creation": {
        "description": "Create a VLAN and assign a name.",
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "required": True, "type": "number", "placeholder": "10"},
            {"name": "vlan_name", "label": "VLAN Name", "required": True, "type": "text", "placeholder": "USERS"},
        ],
    },
    "access_port_assignment": {
        "description": "Set interface as access port and assign VLAN.",
        "fields": [
            {"name": "interface", "label": "Interface", "required": True, "type": "text", "placeholder": "GigabitEthernet1/0/10"},
            {"name": "vlan_id", "label": "VLAN ID", "required": True, "type": "number", "placeholder": "10"},
            {"name": "description", "label": "Description", "required": False, "type": "text", "placeholder": "User Desk"},
        ],
    },
    "trunk_setup": {
        "description": "Set interface as trunk with allowed/native VLANs.",
        "fields": [
            {"name": "interface", "label": "Interface", "required": True, "type": "text", "placeholder": "GigabitEthernet1/0/48"},
            {"name": "allowed_vlans", "label": "Allowed VLANs", "required": True, "type": "text", "placeholder": "10,20,30"},
            {"name": "native_vlan", "label": "Native VLAN", "required": False, "type": "number", "placeholder": "99"},
        ],
    },
    "static_route": {
        "description": "Configure a static route.",
        "fields": [
            {"name": "destination_network", "label": "Destination Network", "required": True, "type": "text", "placeholder": "10.50.0.0"},
            {"name": "subnet_mask", "label": "Subnet Mask", "required": True, "type": "text", "placeholder": "255.255.0.0"},
            {"name": "next_hop", "label": "Next Hop", "required": True, "type": "text", "placeholder": "192.168.1.254"},
        ],
    },
}


_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "config_templates"
_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def available_templates() -> list[str]:
    return sorted(TEMPLATE_MAP.keys())


def template_schema_map() -> dict[str, dict]:
    return {name: TEMPLATE_SCHEMA.get(name, {"description": "", "fields": []}) for name in available_templates()}


def render_template_commands(template_name: str, variables: dict[str, Any] | None = None) -> list[str]:
    if template_name not in TEMPLATE_MAP:
        raise TemplateNotAvailableError(
            f"Unknown template '{template_name}'. Available templates: {', '.join(available_templates())}"
        )

    variables = variables or {}
    filename = TEMPLATE_MAP[template_name]

    try:
        template = _ENV.get_template(filename)
    except TemplateNotFound as exc:
        raise TemplateNotAvailableError(f"Template file not found: {filename}") from exc

    try:
        rendered = template.render(**variables)
    except Exception as exc:
        raise TemplateRenderError(str(exc)) from exc

    commands: list[str] = []
    for line in rendered.splitlines():
        command = line.strip()
        if not command:
            continue
        if command.startswith("#"):
            continue
        commands.append(command)

    return commands
