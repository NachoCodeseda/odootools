"""Find the shortest relational paths between two models (BFS over fields_get)."""
from collections import deque


def pathfinder(env, origin, destination):
    """Return every shortest path from origin to destination.

    Each path is a list of (model, field, field_type) steps; the first step is
    (origin, 'self', '').
    """
    missing = [m for m in (origin, destination) if m not in env]
    if missing:
        raise ValueError(f"Model(s) not found in database: {', '.join(missing)}")

    start = [(origin, 'self', '')]
    if origin == destination:
        return [start]

    queue = deque([start])
    explored = {origin}
    found = []
    while queue:
        path = queue.popleft()
        if found and len(path) >= len(found[0]):
            break  # all remaining paths are longer than the shortest ones
        fields = env[path[-1][0]].fields_get(attributes=['relation', 'type'])
        for name, field in fields.items():
            relation = field.get('relation')
            if not relation:
                continue
            step = path + [(relation, name, field['type'])]
            if relation == destination:
                found.append(step)
            elif relation not in explored and not found:
                explored.add(relation)
                queue.append(step)
    return found


def format_path(path):
    """Render a path as 'origin.field1.field2 (N steps, one2many)' plus one line per step."""
    types = [t for _, _, t in path if t]
    many_from = any(t.startswith('many') for t in types)
    many_to = any(t.endswith('many') for t in types)
    cardinality = f"{'many' if many_from else 'one'}2{'many' if many_to else 'one'}"
    chain = '.'.join(field for _, field, _ in path)
    lines = [f"{chain}  ({len(path) - 1} steps, {cardinality})"]
    for i, (model, field, type_) in enumerate(path):
        lines.append(f"  {i:>2}  {field:<30} {model:<35} {type_}")
    return '\n'.join(lines)

