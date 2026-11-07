"""Consolidate native resource records without decoding their payloads."""

import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path


def _u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0]


def _resource_header(source, path):
    header = source.read(124)
    if len(header) != 124 or header[:8] != b'IDCL\x0c\0\0\0':
        raise ValueError('Unsupported native resource bank: {value_0}'.format(value_0=f'{path.name}'))
    return header


class ResourceIndex:
    def _read_names(self):
        count = _u64(self.index, self.names_at)
        strings_at = self.names_at + 8 + count * 8
        if strings_at > self.names_end:
            raise ValueError('Invalid native resource string table')
        self.names = []
        for i in range(count):
            start = strings_at + _u64(self.index, self.names_at + 8 + i * 8)
            end = self.index.find(b'\0', start, self.names_end)
            if not strings_at <= start <= end:
                raise ValueError('Invalid native resource string')
            self.names.append(self.index[start:end].decode('utf-8'))

    def _read_metadata(self):
        self.metadata = []
        if self.names_end + 32 * _u32(self.header, 36) != self.lookup_at:
            raise ValueError('Invalid native resource metadata table')
        for i in range(_u32(self.header, 36)):
            row = self.index[self.names_end + i * 32:self.names_end + (i + 1) * 32]
            first, second = _u64(row, 0), _u64(row, 8)
            if max(first, second) >= len(self.names):
                raise ValueError('Invalid native resource metadata name')
            self.metadata.append((self.names[first], self.names[second], row[16:].hex()))

    def _read_records(self, size):
        metadata_tokens = [json.dumps(value, separators=(',', ':')).encode('utf-8') for value in self.metadata]
        self.records = []
        for i in range(self.count):
            row = self.index[self.info_at + i * 144:self.info_at + (i + 1) * 144]
            local = self.ids_at + _u64(row, 32) * 8
            def name(field, local=local, row=row):
                pointer = local + _u64(row, field) * 8
                if not self.ids_at <= pointer < self.idcl_at:
                    raise ValueError('Invalid native resource name reference')
                return self.names[_u64(self.index, pointer)]
            kind, filename = name(0), name(8)
            offset, stored = _u64(row, 56), _u64(row, 64)
            type_start, type_count = _u64(row, 24), _u32(row, 132)
            if type_start + type_count > _u32(self.header, 40):
                raise ValueError('Invalid native resource type reference')
            types = hashlib.sha256(b'\0'.join(metadata_tokens[value]
                for value in self.types[type_start:type_start + type_count])).hexdigest()
            key = (kind, filename, types, _u64(row, 80) if kind == 'renderProgResource' else 0)
            if offset < self.data_at or offset + stored > size:
                raise ValueError('Invalid or duplicate native resource record: {value_0}: {value_1}: {value_2}'.format(value_0=f'{self.path.name}', value_1=f'{key}', value_2=f'{i}'))
            self.records.append((key, (kind, row, i)))

    def __init__(self, path):
        self.path = Path(path)
        with self.path.open('rb') as source:
            self.header = _resource_header(source, self.path)
            self.count = _u32(self.header, 32)
            self.names_at, self.names_end = (_u64(self.header, n) for n in (64, 72))
            self.info_at, self.lookup_at, self.data_at = (_u64(self.header, n) for n in (80, 96, 104))
            self.ids_at = self.lookup_at + 4 * _u32(self.header, 40)
            self.idcl_at = _u64(self.header, 116)
            size = self.path.stat().st_size
            if not 124 <= self.info_at <= self.names_at <= self.names_end <= self.lookup_at <= self.ids_at <= self.idcl_at <= self.data_at <= size:
                raise ValueError('Invalid native resource index boundaries')
            if self.info_at + self.count * 144 != self.names_at or self.data_at > 64 * 1024 * 1024:
                raise ValueError('Invalid native resource index size')
            source.seek(0)
            self.index = source.read(self.data_at)
        self._read_names()
        self._read_metadata()
        self.types = struct.unpack_from('<' + 'I' * _u32(self.header, 40), self.index, self.lookup_at)
        if any(value >= len(self.metadata) for value in self.types):
            raise ValueError('Invalid native resource metadata reference')
        self._read_records(size)


def resource_records(indices, selected, masks):
    result = {}
    for number in sorted(selected, reverse=True):
        if number not in indices:
            continue
        index = indices[number]
        bits = masks[index.path]
        for key, (_, row, position) in index.records:
            if position // 8 < len(bits) and bits[position // 8] & (1 << (position % 8)):
                result.setdefault(key, (index, row, position))
    return result


def _resource_aliases(available, defaultdict):
    aliases = defaultdict(set)
    images = defaultdict(set)
    for key in available:
        name = key[1]
        aliases[name].add(key)
        if key[0] == 'image':
            images[name.split('$', 1)[0].casefold()].add(key)
        if name.startswith('generated/decls/') and name.endswith('.decl'):
            aliases[name.split('/', 3)[3][:-5]].add(key)
    return aliases, images

def _resource_decl_dependencies(key, row, index, readers, stack, pending):
    if key[0] == 'rs_streamfile' and key[1].endswith('.decl'):
        if _u64(row, 64) != _u64(row, 72) or _u32(row, 112) != 0:
            raise ValueError('Expected decoded native declaration: ' + key[1])
        if index.path not in readers:
            readers[index.path] = stack.enter_context(index.path.open('rb'))
        source = readers[index.path]
        source.seek(_u64(row, 56))
        payload = source.read(_u64(row, 64))
        if len(payload) != _u64(row, 64):
            raise ValueError('Truncated native declaration')
        # native shader strings and legacy comment bytes are not JSON.
        for match in re.finditer(r'"(?:[^"\\]|\\.)*"', payload.decode('utf-8-sig', errors='replace')):
            try:
                pending.add(json.loads(match[0]))
            except json.JSONDecodeError:
                continue

def _select_resource_token(token, aliases, images, visited, local, available, selected, stack, readers, pending, streams):
    for key in set(aliases.get(token, ())) | images.get(token.casefold(), set()):
        if key in visited:
            continue
        # shared materials can reference images stored only in the donor map.
        if key in local and not (key[0] == 'rs_streamfile' and key[1].startswith('generated/decls/material2/')):
            continue
        visited.add(key)
        index, row, position = available[key]
        if key not in local:
            selected[key] = available[key]
        for value in index.types[_u64(row, 24):_u64(row, 24) + _u32(row, 132)]:
            kind, name, _ = index.metadata[value]
            pending.add(name)
            streams |= kind == 'rs_emb_sfile' and name not in aliases
        _resource_decl_dependencies(key, row, index, readers, stack, pending)
    return streams

def select_resources(available, local, roots):
    from collections import defaultdict
    aliases, images = _resource_aliases(available, defaultdict)
    selected, streams, visited = {}, False, set()
    pending = set(roots)
    with ExitStack() as stack:
        readers = {}
        while pending:
            token = pending.pop()
            streams = _select_resource_token(token, aliases, images, visited, local, available, selected, stack, readers, pending, streams)
    return selected, streams


def resource_signature(value):
    index, row, _ = value
    with index.path.open('rb') as source:
        source.seek(_u64(row, 56))
        digest, remaining = hashlib.sha256(), _u64(row, 64)
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError('Truncated native resource payload')
            digest.update(chunk)
            remaining -= len(chunk)
    return row[40:56], row[64:88], row[96:144], digest.digest()


def _bank_index(selected):
    entries = sorted(selected.items())
    names, metadata, types, ids, rows = [], [], [], [], bytearray()
    name_ids, metadata_ids = {}, {}
    def name_id(value):
        if value not in name_ids:
            name_ids[value] = len(names)
            names.append(value)
        return name_ids[value]
    for key, (index, original, _) in entries:
        row = bytearray(original)
        struct.pack_into('<QQ', row, 0, 0, 1)
        struct.pack_into('<QQ', row, 24, len(types), len(ids))
        ids.extend((name_id(key[0]), name_id(key[1])))
        for value in index.types[_u64(original, 24):_u64(original, 24) + _u32(original, 132)]:
            item = index.metadata[value]
            if item not in metadata_ids:
                metadata_ids[item] = len(metadata)
                first, second, opaque = item
                metadata.append(struct.pack('<QQ', name_id(first), name_id(second)) + bytes.fromhex(opaque))
            types.append(metadata_ids[item])
        rows.extend(row)
    offsets, strings = [], bytearray()
    for name in names:
        offsets.append(len(strings))
        strings.extend(name.encode('utf-8') + b'\0')
    strings = struct.pack('<Q', len(names)) + struct.pack('<' + 'Q' * len(names), *offsets) + strings
    header = bytearray(entries[0][1][0].header)
    names_at = 124 + len(rows)
    names_end = names_at + len(strings)
    lookup_at = names_end + 32 * len(metadata)
    idcl_at = lookup_at + 4 * len(types) + 8 * len(ids)
    data_at = (idcl_at + 4 + 65535) & ~65535
    for at, value in ((32, len(entries)), (36, len(metadata)), (40, len(types)), (44, len(ids)), (56, len(strings))):
        struct.pack_into('<I', header, at, value)
    for at, value in ((64, names_at), (72, names_end), (80, 124), (88, names_end),
                      (96, lookup_at), (104, data_at), (116, idcl_at)):
        struct.pack_into('<Q', header, at, value)
    prefix = (header + rows + strings + b''.join(metadata)
              + struct.pack('<' + 'I' * len(types), *types)
              + struct.pack('<' + 'Q' * len(ids), *ids)
              + b'IDCL' + bytes(data_at - idcl_at - 4))
    return entries, rows, prefix


def _bank_payloads(stream, entries, rows):
    with ExitStack() as stack:
        readers = {}
        for number, (_, (index, row, _)) in enumerate(entries):
            stream.write(bytes((-stream.tell()) % 16))
            struct.pack_into('<Q', rows, number * 144 + 56, stream.tell())
            if index.path not in readers:
                readers[index.path] = stack.enter_context(index.path.open('rb'))
            source = readers[index.path]
            source.seek(_u64(row, 56))
            remaining = _u64(row, 64)
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('Truncated native resource payload')
                stream.write(chunk)
                remaining -= len(chunk)
    stream.seek(124)
    stream.write(rows)


def plan_resource_bank(selected, staging):
    from .install_journal import checked_path

    entries, rows, prefix = _bank_index(selected)
    staging = Path(staging)
    checked_path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=staging, suffix='.incoming', delete=False) as stream:
        incoming = Path(stream.name)
        written = False
        try:
            stream.write(prefix)
            _bank_payloads(stream, entries, rows)
            stream.flush()
            os.fsync(stream.fileno())
            written = True
        finally:
            if not written:
                stream.close()
                incoming.unlink(missing_ok=True)
    try:
        verified = ResourceIndex(incoming)
        if [key for key, _ in verified.records] != [key for key, _ in entries]:
            raise ValueError('Selected native resource index differs')
        with incoming.open('rb') as source:
            identity = hashlib.file_digest(source, 'sha256').hexdigest()
        target = staging / (identity + '.resources')
        incoming.replace(target)
    finally:
        incoming.unlink(missing_ok=True)
    info = dict(size=target.stat().st_size, records=len(entries), identity=identity,
                base_count=0, base_mask='', format=3)
    return target, info


def verify_resource_bank(path, info):
    from .install_journal import checked_path, file_digest

    checked_path(path)
    if path.stat().st_size != info['size'] or file_digest(path) != info['identity']:
        raise ValueError('Generated resource bank integrity differs')
    if len(ResourceIndex(path).records) != info['records']:
        raise ValueError('Selected native resource index differs')


def publish_resource_bank(source, target, info, *, journal=None):
    from doom_eap.launcher.launcher_platform import _atomic_write_bytes

    from .install_journal import checked_path

    checked_path(target)
    checked_path(target.with_suffix('.json'))
    verify_resource_bank(source, info)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        verify_resource_bank(target, info)
    elif journal is not None:
        journal.replace_file(target, source, operation='resource_bank')
    else:
        from doom_eap.launcher.launcher_platform import publish_file
        publish_file(source, target, operation='resource_bank')
    receipt = {**info, 'mtime_ns': target.stat().st_mtime_ns}
    payload = json.dumps(receipt).encode('utf-8')
    if journal is None:
        _atomic_write_bytes(target.with_suffix('.json'), payload)
    else:
        journal.replace(target.with_suffix('.json'), payload, operation='resource_bank_receipt')
    return receipt


def write_resource_bank(selected, cache):
    with tempfile.TemporaryDirectory(dir=Path(cache).parent, prefix='.doomeap-bank-') as directory:
        source, info = plan_resource_bank(selected, directory)
        target = Path(cache) / source.name
        return target, publish_resource_bank(source, target, info)


def mask_tables(mask):
    if len(mask) < 8:
        raise ValueError('Truncated native resource mask')
    tables, at = {}, 8
    for _ in range(_u32(mask, 4)):
        if at + 12 > len(mask):
            raise ValueError('Truncated native resource mask')
        end = at + 12 + 8 * _u32(mask, at + 8)
        if end > len(mask):
            raise ValueError('Truncated native resource mask')
        tables[at] = mask[at + 12:end]
        at = end
    if at != len(mask):
        raise ValueError('Invalid native resource mask boundary')
    return tables


def read_masks(base):
    from .campaign_resources import _records, resource_mapping_lines
    mask = _records(base / 'meta.resources', {'generated/buildgame/container.mask'})['generated/buildgame/container.mask'][3]
    tables = mask_tables(mask)
    result = {}
    mapping = ''.join(resource_mapping_lines(mask, (base / 'idRehash.map').read_text(encoding='utf-8-sig')))
    for name, offset in _bank_mapping(mask, mapping).items():
        result[name] = tables[offset]
    return result


def _bank_mapping(mask, mapping):
    tables = mask_tables(mask)
    result = {}
    offsets = set()
    for line in mapping.splitlines():
        name, offset = line.rsplit(';', 1)
        name, offset = name.replace('\\', '/').removeprefix('./'), int(offset)
        if name in result or offset in offsets or offset not in tables:
            raise ValueError('Ambiguous or invalid native resource mask mapping')
        result[name] = offset
        offsets.add(offset)
    return result


def _bank_bits(info):
    bits = bytearray((info['records'] + 63) // 64 * 8)
    previous = bytes.fromhex(info['base_mask'])
    bits[:min(len(bits), len(previous))] = previous[:len(bits)]
    for index in range(info['base_count'], info['records']):
        bits[index // 8] |= 1 << (index % 8)
    return bits


def _verify_bank_tables(mask, mapping, banks, paths):
    offsets = _bank_mapping(mask, mapping)
    tables = mask_tables(mask)
    for name, info in banks.items():
        verify_resource_bank(paths[name], info)
        offset = offsets.get(name)
        if offset is None or not _u64(mask, offset):
            raise ValueError('Native resource header registration is empty: ' + name)
        if tables[offset] != _bank_bits(info):
            raise ValueError('Native resource bank mask differs: ' + name)


def verify_bank_registration(meta, mapping, spec, report, *, bank_paths):
    from .campaign_resources import _records
    from .native_enemy_resources import _check_spec_refs

    banks = report.get('consolidated', {})
    if not banks:
        return
    _, kind, _, mask, flags = _records(meta, {'generated/buildgame/container.mask'})['generated/buildgame/container.mask']
    if kind != 'rs_streamfile' or flags:
        raise ValueError('Expected uncompressed native resource mask')
    _verify_bank_tables(mask, mapping, banks, bank_paths)
    _check_spec_refs(spec)
    refs = {(spec['maps'][ref['map']]['name'], spec['files'][ref['file']]['name']) for ref in spec['mapFileRefs']}
    referenced = {name for _, name in refs}
    expected = {(target, name) for target, names in report['maps'].items() for name in names}
    if not set(banks) <= referenced or not expected <= refs:
        raise ValueError('Native resource bank package reference differs')


def _obsolete_bank_receipt(receipt, path, name, result):
    try:
        data = receipt.read_bytes()
        info = json.loads(data)
        stat = path.stat()
        with path.open('rb') as source:
            header = source.read(124)
        if (info.get('identity') != receipt.stem or info.get('size') != stat.st_size
                or info.get('mtime_ns') != stat.st_mtime_ns or header[:8] != b'IDCL\x0c\0\0\0'
                or info.get('records') != _u32(header, 32)):
            return
        if info.get('format') != 3 and info.get('records') != info.get('base_count', -1) + info.get('appended', -1):
            return
        result[name] = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                            receipt_sha256=hashlib.sha256(data).hexdigest())
    except (OSError, ValueError, TypeError):
        return

def obsolete_banks(base, spec):
    directory = base / 'ap_enemy_resources'
    if not directory.is_dir():
        return {}
    if directory.resolve() != base.resolve() / directory.name:
        raise ValueError('Generated resource directory is redirected')
    active = {value['name'] for value in spec['files']}
    result = {}
    for receipt in directory.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{64}', receipt.stem) or receipt.is_symlink():
            continue
        path = receipt.with_suffix('.resources')
        name = path.relative_to(base).as_posix()
        if name in active or path.is_symlink() or not path.is_file():
            continue
        _obsolete_bank_receipt(receipt, path, name, result)
    return result


def cleanup_banks(base, obsolete):
    from .build_manifest import decode_manifest
    spec = json.loads((base / 'packagemapspec.json').read_bytes())
    active = {value['name'] for value in spec['files']}
    registered = read_masks(base)
    manifest, _ = decode_manifest((base / 'build-manifest.bin').read_bytes())
    removed, errors, freed = [], [], 0
    for name, before in obsolete.items():
        if name in active or name in registered or name in manifest['files']:
            continue
        try:
            size = _remove_obsolete_bank(base, name, before)
            removed.append(name)
            freed += size
        except OSError as error:
            errors.append(str(error))
    return dict(removed=removed, bytes_freed=freed, errors=errors)


def _remove_obsolete_bank(base, name, before):
    from .install_journal import checked_path, file_digest

    if not re.fullmatch(r'ap_enemy_resources/[0-9a-f]{64}\.resources', name):
        raise OSError('Invalid obsolete resource bank path')
    path = checked_path(base / name)
    receipt = checked_path(path.with_suffix('.json'))
    if not path.exists() and not receipt.exists():
        return 0
    if file_digest(receipt) != before['receipt_sha256']:
        raise OSError('Obsolete resource receipt changed before cleanup')
    size = 0
    if path.exists():
        if (path.stat().st_size != before['size'] or path.stat().st_mtime_ns != before['mtime_ns']
                or file_digest(path) != path.stem):
            raise OSError('Obsolete resource bank changed before cleanup')
        size = before['size']
        path.unlink()
    receipt.unlink()
    return size


def _remove_obsolete_bank_mask(mapping, payload, obsolete):
    lines = [line.rsplit(';', 1) for line in mapping.splitlines()]
    removed = {int(offset) for name, offset in lines if name.replace('\\', '/').removeprefix('./') in obsolete}
    mask, remap, at = bytearray(payload[:8]), {}, 8
    for _ in range(_u32(payload, 4)):
        end = at + 12 + 8 * _u32(payload, at + 8)
        if at not in removed:
            remap[at] = len(mask)
            mask.extend(payload[at:end])
        at = end
    struct.pack_into('<I', mask, 4, _u32(payload, 4) - len(removed))
    mapping = ''.join(line if int(offset) == remap[int(offset)] else f'{name};{remap[int(offset)]}\n'
                      for line, (name, offset) in zip(mapping.splitlines(keepends=True), lines, strict=True)
                      if int(offset) not in removed)
    return mask, mapping

def _append_bank_mask(mapping, banks, mask, entries):
    known = {line.rsplit(';', 1)[0].replace('\\', '/').removeprefix('./') for line in mapping.splitlines()}
    for name, info in banks.items():
        if name in known:
            continue
        offset = len(mask)
        bits = _bank_bits(info)
        mask.extend(b'\0' * 8 + struct.pack('<I', len(bits) // 8) + bits)
        entries.append((name, offset))
    mapping = mapping + ('\n' if mapping and not mapping.endswith('\n') else '')
    mapping += ''.join(f'{name};{offset}\n' for name, offset in entries)
    struct.pack_into('<I', mask, 4, _u32(mask, 4) + len(entries))
    return mapping

def _publish_bank_tables(original, mask, number, meta, entries, base, directory, bank_paths, banks, paths, mapping, updates, encoding, _records):
    updated = bytearray(original)
    at = (len(updated) + 15) & ~15
    updated.extend(b'\0' * (at - len(updated)))
    updated.extend(mask)
    struct.pack_into('<QQQ', updated, _u64(updated, 80) + number * 144 + 56, at, len(mask), len(mask))
    meta.write_bytes(updated)
    if entries:
        _rehash_banks(base, directory, entries, bank_paths or {})
    verified = meta.read_bytes()
    actual = _records(meta, {'generated/buildgame/container.mask'})['generated/buildgame/container.mask'][3]
    _verify_rehashed_mask(mask, actual, entries)
    _verify_bank_tables(actual, mapping, banks, paths)
    updates['meta.resources'] = verified
    updates['idRehash.map'] = mapping.encode(encoding)

def register_banks(base, banks, updates, obsolete=(), *, bank_paths=None):
    from .campaign_resources import _records, resource_mapping_lines
    if not banks and not obsolete:
        return
    original = updates.get('meta.resources', (base / 'meta.resources').read_bytes())
    original_mapping = updates.get('idRehash.map', (base / 'idRehash.map').read_bytes())
    mapping = original_mapping.decode('utf-8-sig')
    encoding = 'utf-8-sig' if original_mapping.startswith(b'\xef\xbb\xbf') else 'utf-8'
    paths = {name: Path((bank_paths or {}).get(name, base / name)) for name in banks}
    with tempfile.TemporaryDirectory(prefix='.doomeap-banks-', dir=base) as directory:
        directory = Path(directory)
        meta = directory / 'meta.resources'
        meta.write_bytes(original)
        number, _, _, payload, flags = _records(meta, {'generated/buildgame/container.mask'})['generated/buildgame/container.mask']
        if flags:
            raise ValueError('Expected uncompressed native resource mask')
        mapping = ''.join(resource_mapping_lines(payload, mapping))
        _bank_mapping(payload, mapping)
        mask, mapping = _remove_obsolete_bank_mask(mapping, payload, obsolete)
        entries = []
        mapping = _append_bank_mask(mapping, banks, mask, entries)
        if not entries and mask == payload:
            _verify_bank_tables(mask, mapping, banks, paths)
            return
        _publish_bank_tables(original, mask, number, meta, entries, base, directory, bank_paths, banks, paths, mapping, updates, encoding, _records)


def _verify_rehashed_mask(mask, actual, entries):
    for _, offset in entries:
        if not any(actual[offset:offset + 8]):
            raise ValueError('Native resource header registration is empty')
        mask[offset:offset + 8] = actual[offset:offset + 8]
    if actual != mask:
        raise ValueError('Native resource registration changed unrelated mask records')


def _rehash_banks(base, directory, entries, bank_paths):
    (directory / 'idRehash.map').write_text(''.join(
        f'{bank_paths.get(name, base / name).resolve()};{offset}\n' for name, offset in entries), encoding='utf-8')
    tool = base / ('idRehash.exe' if os.name == 'nt' else 'idRehash')
    result = subprocess.run([str(tool.resolve())], cwd=directory, capture_output=True, timeout=120,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('Native resource header registration failed: ' + result.stderr.decode(errors='replace')[-1000:])
