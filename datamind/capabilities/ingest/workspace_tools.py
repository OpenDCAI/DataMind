"""Workspace publication tools, bound lazily so reads never mutate a snapshot."""
from datamind.core.tools import ToolSpec


def build_workspace_tools(settings, active):
    enabled = sorted(set(active) & {'kb', 'graph', 'db'})
    if not enabled:
        return []

    async def update(path: str, surfaces: list[str] | None = None, relation_types=None):
        from datamind.workspace import Workspace
        selected = enabled if surfaces is None else surfaces
        if not selected or not set(selected) <= set(enabled):
            raise ValueError('requested workspace surfaces are not enabled')
        return await Workspace(settings).update(path, surfaces=selected, relation_types=relation_types)

    return [ToolSpec(
        name='workspace_update',
        description=('Build and publish a complete workspace revision over selected enabled surfaces. '
                     'Reuses source checkpoints; failed builds preserve the previous published revision. '
                     'Currently requires local snapshot-capable providers; does not update unversioned live stores.'),
        input_schema={'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'surfaces': {'type': 'array', 'items': {'type': 'string', 'enum': enabled}},
            'relation_types': {'type': 'array', 'items': {'type': 'string'}},
        }, 'required': ['path']}, handler=update,
        metadata={'group': 'ingest', 'surface': enabled[0], 'access': 'write', 'multi_surface': True},
    )]
