"""Consolidate native resource records without decoding their payloads."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import os
import tempfile


def _u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0]


class ResourceIndex:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open('rb') as source:
            self.header = source.read(124)
            if self.header[:8] != b'IDCL\x0c\0\0\0':
                raise ValueError(f'Unsupported native resource bank: {self.path.name}')
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
        self.metadata = []
        if self.names_end + 32 * _u32(self.header, 36) != self.lookup_at:
            raise ValueError('Invalid native resource metadata table')
        for i in range(_u32(self.header, 36)):
            row = self.index[self.names_end + i * 32:self.names_end + (i + 1) * 32]
            first, second = _u64(row, 0), _u64(row, 8)
            if max(first, second) >= len(self.names):
                raise ValueError('Invalid native resource metadata name')
            self.metadata.append((self.names[first], self.names[second], row[16:].hex()))
        self.types = struct.unpack_from('<' + 'I' * _u32(self.header, 40), self.index, self.lookup_at)
        if any(value >= len(self.metadata) for value in self.types):
            raise ValueError('Invalid native resource metadata reference')
        metadata_tokens = [json.dumps(value, separators=(',', ':')).encode('utf-8') for value in self.metadata]
        self.records = []
        for i in range(self.count):
            row = self.index[self.info_at + i * 144:self.info_at + (i + 1) * 144]
            local = self.ids_at + _u64(row, 32) * 8
            def name(field):
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
                raise ValueError(f'Invalid or duplicate native resource record: {self.path.name}: {key}: {i}')
            self.records.append((key, (kind, row, i)))


def consolidate_bank(destination, donors, excluded, cache, enabled):
    """Keep destination records and append only missing donor names in priority order."""
    indices = [ResourceIndex(path) for path in (destination, *donors)]
    identity = [(str(i.path), i.path.stat().st_size, i.path.stat().st_mtime_ns,
                 hashlib.sha256(i.index).hexdigest()) for i in indices]
    cache_key = hashlib.sha256(json.dumps([2, identity, sorted(excluded),
        [enabled[str(i.path)].hex() for i in indices]], separators=(',', ':')).encode()).hexdigest()
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (cache_key + '.resources')
    receipt = target.with_suffix('.json')
    if target.is_file() and receipt.is_file():
        stored = json.loads(receipt.read_text())
        stat = target.stat()
        if stored.get('size') == stat.st_size and stored.get('mtime_ns') == stat.st_mtime_ns:
            return target, stored
    base = indices[0]
    base_bits = enabled[str(base.path)]
    selected, seen = [], {key for key, (_, _, i) in base.records
                          if i // 8 < len(base_bits) and base_bits[i // 8] & (1 << (i % 8))} | set(excluded)
    for donor in indices[1:]:
        bits = enabled[str(donor.path)]
        for key, (kind, row, number) in donor.records:
            name = key[1]
            if number // 8 < len(bits) and bits[number // 8] & (1 << (number % 8)) and key not in seen:
                selected.append((donor, name, kind, row))
                seen.add(key)
    names = list(base.names)
    name_ids = {name: i for i, name in enumerate(names)}
    for _, name, kind, _ in selected:
        for value in (kind, name):
            if value not in name_ids:
                name_ids[value] = len(names)
                names.append(value)
    for donor in indices[1:]:
        for first, second, _ in donor.metadata:
            for value in (first, second):
                if value not in name_ids:
                    name_ids[value] = len(names)
                    names.append(value)
    offsets, strings = [], bytearray()
    for name in names:
        offsets.append(len(strings))
        strings.extend(name.encode('utf-8') + b'\0')
    encoded_names = struct.pack('<Q', len(names)) + struct.pack('<' + 'Q' * len(offsets), *offsets) + strings
    unknown = bytearray(base.index[base.names_end:base.lookup_at])
    types = bytearray(base.index[base.lookup_at:base.ids_at])
    type_offsets = {}
    for donor in indices[1:]:
        type_offsets[donor.path] = len(types) // 4
        metadata_offset = len(unknown) // 32
        for first, second, opaque in donor.metadata:
            unknown.extend(struct.pack('<QQ', name_ids[first], name_ids[second]) + bytes.fromhex(opaque))
        types.extend(struct.pack('<' + 'I' * len(donor.types),
                                 *(value + metadata_offset for value in donor.types)))
    ids = bytearray(base.index[base.ids_at:base.idcl_at])
    idcl = base.index[base.idcl_at:base.data_at]
    rows = bytearray(base.index[base.info_at:base.names_at])
    for donor, name, kind, original in selected:
        row = bytearray(original)
        struct.pack_into('<QQ', row, 0, 0, 1)
        struct.pack_into('<Q', row, 32, len(ids) // 8)
        struct.pack_into('<Q', row, 24, _u64(row, 24) + type_offsets[donor.path])
        ids.extend(struct.pack('<QQ', name_ids[kind], name_ids[name]))
        rows.extend(row)
    names_at = base.info_at + len(rows)
    names_end = names_at + len(encoded_names)
    lookup_at = names_end + len(unknown)
    ids_at = lookup_at + len(types)
    idcl_at = ids_at + len(ids)
    data_at = idcl_at + len(idcl)
    delta = data_at - base.data_at
    header = bytearray(base.index[:base.info_at])
    struct.pack_into('<I', header, 32, base.count + len(selected))
    struct.pack_into('<I', header, 36, len(unknown) // 32)
    struct.pack_into('<I', header, 40, len(types) // 4)
    struct.pack_into('<I', header, 44, _u32(header, 44) + 2 * len(selected))
    struct.pack_into('<I', header, 56, len(encoded_names))
    for offset, value in ((64, names_at), (72, names_end), (88, names_end), (96, lookup_at), (104, data_at), (116, idcl_at)):
        struct.pack_into('<Q', header, offset, value)
    for i in range(base.count):
        struct.pack_into('<Q', rows, i * 144 + 56, _u64(rows, i * 144 + 56) + delta)
    with tempfile.NamedTemporaryFile(dir=cache, suffix='.incoming', delete=False) as stream:
        incoming = Path(stream.name)
        try:
            stream.write(header + rows + encoded_names + unknown + types + ids + idcl)
            with base.path.open('rb') as source:
                source.seek(base.data_at)
                shutil.copyfileobj(source, stream, 1024 * 1024)
            with ExitStack() as stack:
                sources = {index.path: stack.enter_context(index.path.open('rb')) for index in indices[1:]}
                for number, (index, name, _, row) in enumerate(selected):
                    padding = (-stream.tell()) % 16
                    stream.write(b'\0' * padding)
                    offset = stream.tell()
                    source = sources[index.path]
                    source.seek(_u64(row, 56))
                    remaining = _u64(row, 64)
                    while remaining:
                        chunk = source.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError('Truncated native resource payload')
                        stream.write(chunk)
                        remaining -= len(chunk)
                    struct.pack_into('<Q', rows, (base.count + number) * 144 + 56, offset)
            stream.seek(base.info_at)
            stream.write(rows)
        except BaseException:
            stream.close()
            incoming.unlink(missing_ok=True)
            raise
    try:
        verified = ResourceIndex(incoming)
        if len(verified.records) != base.count + len(selected):
            raise ValueError('Consolidated native resource index differs')
        incoming.replace(target)
    finally:
        incoming.unlink(missing_ok=True)
    stat = target.stat()
    result = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'records': base.count + len(selected),
              'appended': len(selected), 'identity': cache_key, 'base_count': base.count,
              'base_mask': enabled[str(base.path)].hex()}
    receipt.write_text(json.dumps(result), encoding='utf-8')
    return target, result


def read_masks(base):
    from .campaign_resources import _records
    mask = _records(base / 'meta.resources', {'generated/buildgame/container.mask'})['generated/buildgame/container.mask'][3]
    tables, at = {}, 8
    for _ in range(_u32(mask, 4)):
        end = at + 12 + 8 * _u32(mask, at + 8)
        if end > len(mask):
            raise ValueError('Truncated native resource mask')
        tables[at] = mask[at + 12:end]
        at = end
    if at != len(mask):
        raise ValueError('Invalid native resource mask boundary')
    result = {}
    for line in (base / 'idRehash.map').read_text(encoding='utf-8-sig').splitlines():
        name, offset = line.rsplit(';', 1)
        if int(offset) not in tables:
            raise ValueError('Invalid native resource mask mapping')
        result[name.replace('\\', '/').removeprefix('./')] = tables[int(offset)]
    return result


def shared_donor_bank(base, spec, requested, by_map, maps, masks):
    donors = set().union(*(by_map[maps[name]] for names in requested.values() for name in names))
    native = by_map.get(maps.get('common'), set())
    destinations = set().union(*(by_map[maps[name]] for name in requested))
    paths = {i: base / spec['files'][i]['name'] for i in donors | native | destinations
             if spec['files'][i]['name'].endswith('.resources')}
    indices = {i: ResourceIndex(path) for i, path in paths.items()}

    def records(selected):
        result = {}
        for i in sorted(selected, reverse=True):
            if i not in indices:
                continue
            bits = masks[spec['files'][i]['name']]
            for key, (_, row, number) in indices[i].records:
                if number // 8 < len(bits) and bits[number // 8] & (1 << (number % 8)):
                    result.setdefault(key, (i, row))
        return result

    hashes = {}
    def signature(value):
        i, row = value
        identity = (i, _u64(row, 56), _u64(row, 64))
        if identity not in hashes:
            with paths[i].open('rb') as source:
                source.seek(identity[1])
                digest, remaining = hashlib.sha256(), identity[2]
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError('Truncated native resource payload')
                    digest.update(chunk)
                    remaining -= len(chunk)
                hashes[identity] = digest.digest()
        return row[40:56], row[64:88], row[96:144], hashes[identity]

    incoming = records(donors)
    for target, names in requested.items():
        local = records(by_map[maps[target]] | native)
        expected = records(set().union(*(by_map[maps[name]] for name in names)))
        for key, value in expected.items():
            if key not in local and value != incoming[key] and signature(value) != signature(incoming[key]):
                return None
    common = records(native)
    destination = min((i for i in native if i in paths), key=lambda i: paths[i].stat().st_size)
    enabled = {str(path): masks[spec['files'][i]['name']] for i, path in paths.items()}
    return consolidate_bank(paths[destination], [paths[i] for i in sorted(donors, reverse=True) if i in paths],
                            common, base / 'ap_enemy_resources', enabled)


def register_banks(base, banks, updates):
    from .campaign_resources import _records
    if not banks:
        return
    original = updates.get('meta.resources', (base / 'meta.resources').read_bytes())
    mapping = updates.get('idRehash.map', (base / 'idRehash.map').read_bytes()).decode('utf-8-sig')
    with tempfile.TemporaryDirectory(prefix='.doomeap-banks-', dir=base) as directory:
        directory = Path(directory)
        meta = directory / 'meta.resources'
        meta.write_bytes(original)
        number, _, _, payload, flags = _records(meta, {'generated/buildgame/container.mask'})['generated/buildgame/container.mask']
        if flags:
            raise ValueError('Expected uncompressed native resource mask')
        mask, entries = bytearray(payload), []
        known = {line.rsplit(';', 1)[0].replace('\\', '/') for line in mapping.splitlines()}
        for name, info in banks.items():
            if name in known:
                continue
            offset = len(mask)
            bits = bytearray((info['records'] + 63) // 64 * 8)
            previous = bytes.fromhex(info['base_mask'])
            bits[:min(len(bits), len(previous))] = previous[:len(bits)]
            for index in range(info['base_count'], info['records']):
                bits[index // 8] |= 1 << (index % 8)
            mask.extend(b'\0' * 8 + struct.pack('<I', len(bits) // 8) + bits)
            entries.append((name, offset))
        if not entries:
            return
        struct.pack_into('<I', mask, 4, _u32(mask, 4) + len(entries))
        updated = bytearray(original)
        at = (len(updated) + 15) & ~15
        updated.extend(b'\0' * (at - len(updated)))
        updated.extend(mask)
        struct.pack_into('<QQQ', updated, _u64(updated, 80) + number * 144 + 56, at, len(mask), len(mask))
        meta.write_bytes(updated)
        (directory / 'idRehash.map').write_text(''.join(f'{(base / name).resolve()};{offset}\n' for name, offset in entries), encoding='utf-8')
        tool = base / ('idRehash.exe' if os.name == 'nt' else 'idRehash')
        result = subprocess.run([str(tool.resolve())], cwd=directory, capture_output=True, timeout=120,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('Native resource header registration failed: ' + result.stderr.decode(errors='replace')[-1000:])
        verified = meta.read_bytes()
        actual = _records(meta, {'generated/buildgame/container.mask'})['generated/buildgame/container.mask'][3]
        for _, offset in entries:
            if not any(actual[offset:offset + 8]):
                raise ValueError('Native resource header registration is empty')
            mask[offset:offset + 8] = actual[offset:offset + 8]
        if actual != mask:
            raise ValueError('Native resource registration changed unrelated mask records')
        updates['meta.resources'] = verified
        updates['idRehash.map'] = (mapping.rstrip('\r\n') + '\n' + ''.join(f'{name};{offset}\n' for name, offset in entries)).encode('utf-8')
