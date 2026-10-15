'''
User-facing messages that the browser GUI can show in the language the user selected.

Messages are written in English as a template with named parameters:

    notify(Msg('Point {n} accepted ({name}).', n=3, name='DLH123'))

str(msg) fills in the parameters and gives the English text; this is what the terminal shows.
The browser GUI receives the template and the parameters separately (to_json), looks the template
up in a translation table (webgui_static/i18n_de.js) and fills in the parameters itself. A
template without an entry in the table is shown in English.

Format specifications such as {rms:.2f} are applied here in Python, so the browser only has to
substitute ready-made strings. A parameter may itself be a Msg (for example a direction name),
which is then translated on its own.

Plain strings are allowed wherever a message is expected (see Text); they are shown as they are.
Use them for text that must not be translated, such as names, file paths or error texts from the
operating system.
'''

import string

from typing import Any, Sequence, Union

class Msg:
    '''An English message template plus parameters; see the module docstring.'''

    def __init__(self, template: str, **params: Any):
        self.template = template
        self.params = params

    def __str__(self) -> str:
        return self.template.format(**self.params)

    def __repr__(self) -> str:
        return f'Msg({self.template!r}, **{self.params!r})'

    def __add__(self, other: 'Text') -> 'Msg':
        '''Join two messages with a space; each part is translated on its own.'''
        if other == '':
            return self
        return Msg('{a} {b}', a=self, b=other)

    def to_json(self) -> dict[str, Any]:
        '''{'t': template, 'p': {name: formatted string or nested message}} for the browser GUI.'''
        params: dict[str, Any] = {}
        for _, name, spec, _ in string.Formatter().parse(self.template):
            if not name or name in params:
                continue
            value = self.params[name]
            params[name] = value.to_json() if isinstance(value, Msg) else format(value, spec or '')
        return {'t': self.template, 'p': params}

# Anything that can be shown to the user: a translatable message or plain text.
Text = Union[Msg, str]

def to_json(text: Text) -> Any:
    '''JSON form of a message or plain text for the browser GUI.'''
    return text.to_json() if isinstance(text, Msg) else str(text)

def of_exception(e: BaseException) -> Text:
    '''The message carried by an exception raised with a Msg, otherwise its text.'''
    if e.args and isinstance(e.args[0], Msg):
        return e.args[0]
    return str(e)

def join(parts: Sequence[Text]) -> Text:
    '''Join several messages with spaces (an empty list gives an empty string).'''
    if not parts:
        return ''
    out = parts[0]
    for p in parts[1:]:
        out = (out if isinstance(out, Msg) else Msg('{a}', a=out)) + p
    return out
