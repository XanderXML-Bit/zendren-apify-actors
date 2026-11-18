"""Reject byte/depth/node bombs before standard JSON deserialization."""
import json

MAX_INPUT_BYTES = 8_000_000

def decode_bounded(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
        raise ValueError('Input exceeds 8 MB')
    text = raw.decode('utf-8')
    depth = nodes = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1; nodes += 1
            if depth > 6:
                raise ValueError('Input nesting exceeds six levels')
        elif char in ']}':
            depth -= 1
        elif char in ',:':
            nodes += 1
        if nodes > 60_000:
            raise ValueError('Input structural complexity exceeds limits')
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON keys are not allowed')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Non-finite JSON numbers are not allowed')
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
