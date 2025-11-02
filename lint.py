#!/usr/bin/env python3

import copy
import difflib
import itertools
import os.path
import subprocess
import sys
import tempfile

import click
import colorama
import configupdater

SKIP_SECTIONS = [
    lambda s: s == 'presets',
    lambda s: s.startswith('physical_printer:'),
]

SKIP_KEYS = [
    lambda s: s == 'inherits',
    lambda s: s == 'alias',
]

# these keys crash PrusaSlicer
SKIP_RESOLVE_KEYS = [
    # 1.8.0
    lambda s: s == 'wipe_tower_extruder',
    # 2.9.2
    lambda s: s == 'bed_temperature_extruder',
]

def skip(s, matchers):
    return any([f(s) for f in matchers])

def blank_config():
    return configupdater.ConfigUpdater(
        delimiters=['='],
        comment_prefixes=['#'],
    )

def parse_config_bundle(f):
    cfg = blank_config()
    cfg.read_file(f)
    return cfg

def parse_config_dict(f):
    return parse_config_bundle(itertools.chain(['[root]'], f))['root'].to_dict()

def unparse_config_dict(d, f):
    cfg = blank_config()
    cfg.add_section('root')
    for k, v in d.items():
        cfg['root'][k] = v
    prefix = '[root]\n'
    s = str(cfg)
    assert s.startswith(prefix)
    f.write(s[len(prefix):])

class ResolveError(Exception):
    pass

def get_parent(cfg, key):
    parent = cfg[key].get('inherits')
    if parent:
        parent = parent.value
    if parent:
        namespace = ''
        if ':' in key:
            namespace, _ = key.split(':', 1)
            namespace += ':'
        parent = namespace + parent
        if parent not in cfg:
            raise ResolveError('cannot find parent [{}]'.format(parent))
        return parent
    return None

def resolve(cfg, key):
    subdata = cfg[key]
    parent = get_parent(cfg, key)

    if parent:
        resolved = resolve(cfg, parent)
    else:
        resolved = {}

    for k in subdata:
        if skip(k, SKIP_RESOLVE_KEYS):
            continue
        resolved[k] = subdata[k].value

    return resolved

class Slicer:
    def __init__(self):
        self.path = self.discover()

    @classmethod
    def discover(cls):
        for path in cls._discover_paths():
            path = os.path.expanduser(path)
            try:
                subprocess.check_output([path, '--help'], stderr=subprocess.STDOUT)
                return path
            except Exception:
                continue
        raise RuntimeError('cannot find slicer: {}'.format(cls.__name__))

    @classmethod
    def _discover_paths(cls):
        raise NotImplementedError('Slicer._discover_paths')

    def _call(self, *args):
        return subprocess.check_output([self.path] + list(args), stderr=subprocess.STDOUT)

    def normalize_raw(self, cfg, sla=False):
        with tempfile.TemporaryDirectory(prefix='slicerlint.') as d:
            incfg = os.path.join(d, 'input.ini')
            outcfg = os.path.join(d, 'output.ini')

            with open(incfg, 'w') as f:
                unparse_config_dict(cfg, f)

            args = ['--load', incfg, '--save', outcfg]
            if sla:
                args = ['--printer-technology=SLA'] + args

            self._call(*args)

            with open(outcfg) as f:
                normalized = parse_config_dict(f)

            # undo the SLA setting if it's not set in our config
            if sla and 'printer_technology' not in cfg:
                # slicer default is FFF
                normalized['printer_technology'] = 'FFF'

            return normalized

    def normalize(self, cfg, section):
        sla = section.startswith('sla_')
        return self.normalize_raw(resolve(cfg, section), sla=sla)

    def normalize_parent(self, cfg, section):
        sla = section.startswith('sla_') or resolve(cfg, section).get('printer_technology', 'FFF').upper() == 'SLA'
        parent = get_parent(cfg, section)
        base = {}
        if parent:
            base = resolve(cfg, parent)
        return self.normalize_raw(base, sla=sla)

class PrusaSlicer(Slicer):
    @classmethod
    def _discover_paths(cls):
        # always try these
        yield 'prusa-slicer'
        yield 'PrusaSlicer'

        # now for platform-specific ones
        if sys.platform == 'darwin':
            leaf = 'PrusaSlicer.app/Contents/MacOS/PrusaSlicer'
            yield '~/Applications/' + leaf
            yield '/Applications/' + leaf

class Notes:
    ADDED = 'added'
    REMOVED = 'removed'
    CHANGED = 'changed'
    UNKNOWN = 'unknown'
    REDUNDANT = 'redundant'
    FIXED = 'fixed'

    categories = [ADDED, REMOVED, CHANGED, UNKNOWN, REDUNDANT, FIXED]

    def __init__(self):
        self.notes = {k: [] for k in self.categories}

    def add(self, category, section, key='', note=None):
        # remove ADDED if it is later REMOVED
        if category in [self.REMOVED, self.UNKNOWN, self.REDUNDANT]:
            old_added = self.notes[self.ADDED]
            new_added = [n for n in old_added if n['section'] != section or n['key'] != key]
            self.notes[self.ADDED] = new_added
            if old_added != new_added:
                # if added then removed, emit no new note
                return

        self.notes[category].append(dict(
            section=section,
            key=key,
            note=note,
        ))

    def print(self):
        for category in self.categories:
            if not self.notes[category]:
                continue

            if category == self.ADDED:
                print('The following settings have been added:')
            elif category == self.REMOVED:
                print('The following settings have been removed:')
            elif category == self.CHANGED:
                print('The following settings have been changed:')
            elif category == self.UNKNOWN:
                print('The following settings are not known to the slicer')
                print('and have been removed:')
            elif category == self.REDUNDANT:
                print('The following settings are redundant, do not change')
                print('the value from the underlying profile, and have been')
                print('removed:')
            elif category == self.FIXED:
                print('The following settings are modified when loaded by the')
                print('slicer, and have been changed to their final value:')
            else:
                assert False
            print()

            for note in self.notes[category]:
                print(' * [{}]'.format(note['section']), end='')
                if note['key']:
                    print(' {}'.format(note['key']), end='')
                if note['note']:
                    print(' ({})'.format(note['note']))
                else:
                    print()

            print()

def check_same(slicer, original, proposed, ignore_missing=False):
    for section in original:
        if skip(section, SKIP_SECTIONS):
            continue

        if not ignore_missing and not section in proposed:
            raise RuntimeError('section [{}] is missing from proposed config'.format(section))

        co = slicer.normalize(original, section)
        cp = slicer.normalize(proposed, section)

        if co != cp:
            for k in co:
                if skip(k, SKIP_KEYS):
                    continue
                if not k in cp:
                    raise RuntimeError('key [{}] {} is missing in proposed config'.format(section, k))
                if co[k] != cp[k]:
                    raise RuntimeError('key [{}] {} differs in proposed config ({} -> {})'.format(section, k, co[k], cp[k]))
            for k in cp:
                if skip(k, SKIP_KEYS):
                    continue
                if not k in co:
                    raise RuntimeError('key [{}] {} exists only in proposed config'.format(section, k))

    for section in proposed:
        if skip(section, SKIP_SECTIONS):
            continue

        if not ignore_missing and not section in original:
            raise RuntimeError('section [{}] exists only in proposed config'.format(section))
    

def lint(notes, slicer, cfg):
    proposed = copy.deepcopy(cfg)

    for section in cfg:
        if skip(section, SKIP_SECTIONS):
            continue

        normalized = slicer.normalize(cfg, section)
        base = slicer.normalize_parent(cfg, section)

        for k in cfg[section]:
            if skip(k, SKIP_KEYS):
                continue
            elif k not in normalized:
                notes.add(Notes.UNKNOWN, section, k)
                del proposed[section][k]
            elif k in base and normalized[k] == base[k]:
                notes.add(Notes.REDUNDANT, section, k)
                del proposed[section][k]
            elif normalized[k] != cfg[section][k].value:
                notes.add(Notes.FIXED, section, k, '{} -> {}'.format(cfg[section][k].value, normalized[k]))
                proposed[section][k] = normalized[k]

    return proposed

def update(notes, slicer, cfg, target):
    proposed = copy.deepcopy(cfg)

    for section in target:
        if skip(section, SKIP_SECTIONS):
            continue

        if not section in cfg:
            notes.add(Notes.ADDED, section)
            proposed.section_blocks()[-1].add_after.space(1).section(section)
            for k in target[section]:
                proposed[section][k] = target[section][k].value
            continue

        for k in cfg[section]:
            if skip(k, SKIP_KEYS):
                continue

            if k in target[section]:
                if cfg[section][k].value != target[section][k].value:
                    notes.add(Notes.CHANGED, section, k)
                    proposed[section][k] = target[section][k].value
            else:
                # k not in target
                if k in cfg[section]:
                    notes.add(Notes.REMOVED, section, k)
                    del proposed[section][k]

        for k in target[section]:
            if k in cfg[section]:
                continue
            notes.add(Notes.ADDED, section, k)
            proposed[section].option_blocks()[-1].add_after.option(k, target[section][k].value)

    # deleting sections in cfg but not target is non-obvious,
    # as you may delete a section that another section inherits from.
    # So, for now, don't

    return proposed

def output_diff(a, b, filename):
    diff = difflib.unified_diff(str(a).splitlines(), str(b).splitlines(), lineterm='', fromfile='a/' + filename, tofile='b/' + filename)
    differs = False
    for line in diff:
        differs = True
        if not sys.stdout.isatty():
            print(line)
        elif line.startswith('+'):
            print(colorama.Fore.GREEN + line + colorama.Fore.RESET)
        elif line.startswith('-'):
            print(colorama.Fore.RED + line + colorama.Fore.RESET)
        elif line.startswith('@@'):
            print(colorama.Fore.BLUE + line + colorama.Fore.RESET)
        else:
            print(line)
    return differs

@click.command()
@click.option('--write', '-w', is_flag=True, help='write changes to file')
@click.argument('a', metavar='FILE', type=click.File())
@click.argument('b', metavar='[UPDATE]', type=click.File(), required=False)
def main(a, b=None, write=False):
    colorama.just_fix_windows_console()

    slicer = PrusaSlicer()
    notes = Notes()
    cfg = parse_config_bundle(a)
    cfgb = None

    if b:
        target = parse_config_bundle(b)
        updated = update(notes, slicer, cfg, target)
        proposed = lint(notes, slicer, updated)
        check_same(slicer, updated, proposed)
        check_same(slicer, target, proposed, ignore_missing=True)
    else:
        proposed = lint(notes, slicer, cfg)
        check_same(slicer, cfg, proposed)

    notes.print()
    differs = output_diff(cfg, proposed, a.name)

    if write and differs:
        a.close()
        with open(a.name, 'w') as f:
            proposed.write(f)

if __name__ == '__main__':
    main()
