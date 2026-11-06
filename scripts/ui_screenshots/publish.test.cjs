const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { deflateSync } = require('node:zlib');
const matrix = require('./matrix.json');
const { currentPullRequest, imageComment, publish, readImages, readMetadata, storeImages } = require('./publish.cjs');

const meta = { pr: 42, base: 'a'.repeat(40), head: 'b'.repeat(40), headRepository: 'contributor/fitness-tracker' };
const repository = 'owner/fitness-tracker';
const run = {
  event: 'pull_request', conclusion: 'success', path: '.github/workflows/ui-screenshots.yml',
  repository: { full_name: repository }, head_sha: meta.head, head_branch: 'ui-change',
  head_repository: { full_name: meta.headRepository }, pull_requests: [{ number: meta.pr }],
};
const pr = {
  number: 42, state: 'open',
  base: { sha: meta.base, repo: { full_name: repository } },
  head: { sha: meta.head, ref: 'ui-change', repo: { full_name: meta.headRepository } },
};

function temporary(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'fitness-screenshots-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

// Generate real, decodable PNG fixtures with correct chunk checksums.
function png(width, height) {
  function chunk(type, bytes) {
    const name = Buffer.from(type);
    const data = Buffer.concat([name, bytes]);
    let crc = 0xffffffff;
    for (const byte of data) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
    const length = Buffer.alloc(4);
    length.writeUInt32BE(bytes.length);
    const checksum = Buffer.alloc(4);
    checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
    return Buffer.concat([length, data, checksum]);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([
    Buffer.from('89504e470d0a1a0a', 'hex'), chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.alloc((width * 4 + 1) * height))), chunk('IEND', Buffer.alloc(0)),
  ]);
}

function fixture(t) {
  const directory = temporary(t);
  fs.writeFileSync(path.join(directory, 'metadata.json'), JSON.stringify(meta));
  for (const viewport of matrix.viewports) {
    const bytes = png(viewport.width * viewport.scale, viewport.height * viewport.scale);
    for (const side of ['before', 'after']) {
      const folder = path.join(directory, side, viewport.id);
      fs.mkdirSync(folder, { recursive: true });
      for (const page of matrix.pages) fs.writeFileSync(path.join(folder, `${page.id}.png`), bytes);
    }
  }
  return directory;
}

test('matrix covers six pages and all ten device allocations at the right panel sizes', () => {
  assert.equal(matrix.pages.length, 6);
  assert.equal(matrix.viewports.length, 10);
  assert.deepEqual(matrix.groups.map(group => group.label), [
    'Librem 5 · 300%', 'Librem 5 · 200%', 'Mecha Comet · 250%', 'Mecha Comet · 200%', 'Tablet · 100%',
  ]);
  for (const group of matrix.groups) {
    assert.deepEqual(matrix.viewports.filter(viewport => viewport.group === group.id)
      .map(viewport => viewport.orientation).sort(), ['landscape', 'portrait']);
  }
  for (const viewport of matrix.viewports) {
    const physical = [viewport.width * viewport.scale, viewport.height * viewport.scale].sort((a, b) => a - b);
    const expected = viewport.id.startsWith('librem5') ? [720, 1440] :
      viewport.id.startsWith('comet') ? [1080, 1240] : [800, 1280];
    assert.deepEqual(physical, expected);
    assert.match(viewport.id, /^[a-z0-9-]+$/);
  }
});

test('validates every before/after PNG and metadata; ignores untrusted executable extras', t => {
  const directory = fixture(t);
  fs.writeFileSync(path.join(directory, 'evil.cjs'), 'throw new Error("never executed")');
  assert.deepEqual(readMetadata(directory), meta);
  const images = readImages(directory);
  assert.equal(images.length, 120);
  assert.ok(images.every(image => image.relative.endsWith('.png')));
});

test('rejects missing images, bad dimensions, non-PNG data and symlinks', t => {
  const directory = fixture(t);
  const file = path.join(directory, 'before', matrix.viewports[0].id, 'tracker.png');
  const original = fs.readFileSync(file);
  fs.unlinkSync(file);
  assert.throws(() => readImages(directory), /ENOENT/);
  fs.writeFileSync(file, png(10, 10));
  assert.throws(() => readImages(directory), /Invalid PNG or dimensions/);
  fs.writeFileSync(file, Buffer.alloc(100));
  assert.throws(() => readImages(directory), /Invalid PNG or dimensions/);
  fs.writeFileSync(file, original);
  const outside = path.join(directory, 'outside.png');
  fs.renameSync(file, outside);
  fs.symlinkSync(outside, file);
  assert.throws(() => readImages(directory), /Symlink/);
  fs.unlinkSync(file);
  fs.writeFileSync(file, original);
  const parent = path.dirname(file);
  fs.renameSync(parent, `${parent}-real`);
  fs.symlinkSync(`${parent}-real`, parent);
  assert.throws(() => readImages(directory), /Symlink/);
});

test('rejects metadata that could alter publication paths or PR selection', t => {
  const directory = temporary(t);
  for (const patch of [{ pr: '../1' }, { pr: -1 }, { head: 'bad' }, { base: 'bad' }, { headRepository: '../evil' }]) {
    fs.writeFileSync(path.join(directory, 'metadata.json'), JSON.stringify({ ...meta, ...patch }));
    assert.throws(() => readMetadata(directory), /Invalid screenshot metadata/);
  }
});

test('accepts exact same-repository and fork PR runs, including empty fork associations', () => {
  assert.equal(currentPullRequest(run, meta, pr, repository), true);
  assert.equal(currentPullRequest({ ...run, pull_requests: [] }, meta, pr, repository), true);
  const localMeta = { ...meta, headRepository: repository };
  const localPr = { ...pr, head: { ...pr.head, repo: { full_name: repository } } };
  assert.equal(currentPullRequest({ ...run, head_repository: { full_name: repository } }, localMeta, localPr, repository), true);
});

test('rejects unrelated workflow provenance and skips obsolete or closed PRs', () => {
  for (const patch of [
    { event: 'push' }, { conclusion: 'failure' }, { path: '.github/workflows/other.yml' },
    { head_sha: 'c'.repeat(40) }, { repository: { full_name: 'other/repo' } },
    { pull_requests: [{ number: 99 }] },
  ]) assert.throws(() => currentPullRequest({ ...run, ...patch }, meta, pr, repository));
  for (const patch of [
    { state: 'closed' }, { head: { ...pr.head, sha: 'c'.repeat(40) } },
    { base: { ...pr.base, sha: 'c'.repeat(40) } }, { number: 99 },
    { head: { ...pr.head, repo: { full_name: 'other/repo' } } },
  ]) assert.equal(currentPullRequest(run, meta, { ...pr, ...patch }, repository), false);
});

test('comment embeds all 120 images with immutable URLs and revision labels', () => {
  const body = imageComment(meta, 'c'.repeat(40), repository, 'https://github.com/owner/repo/actions/runs/1');
  assert.equal((body.match(/<img /g) || []).length, 120);
  const tables = body.split(/^### /m).slice(1);
  assert.equal(tables.length, 5);
  for (const [index, table] of tables.entries()) {
    const group = matrix.groups[index];
    assert.ok(table.startsWith(`${group.label}\n`));
    assert.equal((table.match(/\| Page \| Before \| After \|/g) || []).length, 1);
    assert.equal((table.match(/<img /g) || []).length, 24);
    const viewports = matrix.viewports.filter(viewport => viewport.group === group.id);
    for (const viewport of viewports) {
      for (const page of matrix.pages) {
        assert.ok(table.includes(`| ${page.label} · ${viewport.orientation} (${viewport.width}×${viewport.height}) |`));
        for (const side of ['before', 'after']) {
          assert.equal(table.split(`/${side}/${viewport.id}/${page.id}.png"`).length - 1, 1);
        }
      }
    }
  }
  assert.ok(body.includes(`/${'c'.repeat(40)}/pr-42/before/`));
  assert.ok(body.includes(meta.base.slice(0, 12)));
  assert.ok(body.includes(meta.head.slice(0, 12)));
  assert.ok(body.length < 65536);
});

test('git storage bootstraps the image branch and preserves other PRs and old image URLs', t => {
  const directory = temporary(t);
  const remote = path.join(directory, 'remote.git');
  execFileSync('git', ['init', '--bare', '--quiet', remote]);
  const image = { relative: 'before/comet-250-portrait/tracker.png', bytes: png(1080, 1240) };
  const first = storeImages({ images: [image], meta, remote });
  const second = storeImages({ images: [image], meta: { ...meta, pr: 43 }, remote });
  const changed = { ...image, bytes: png(720, 1440) };
  const third = storeImages({ images: [changed], meta, remote });
  const git = args => execFileSync('git', ['--git-dir', remote, ...args]);
  assert.equal(git(['rev-parse', 'ui-screenshots']).toString().trim(), third);
  assert.deepEqual(git(['show', `${first}:pr-42/${image.relative}`]), image.bytes);
  assert.deepEqual(git(['show', `${second}:pr-43/${image.relative}`]), image.bytes);
  assert.deepEqual(git(['show', `${third}:pr-42/${image.relative}`]), changed.bytes);
  assert.deepEqual(git(['show', `${third}:pr-43/${image.relative}`]), image.bytes);
  assert.equal(git(['ls-tree', '-r', '--name-only', third]).toString().trim().split('\n').length, 2);
});


test('git storage supplies the token authorization header to Git commands', t => {
  const directory = temporary(t);
  const remote = path.join(directory, 'remote.git');
  execFileSync('git', ['init', '--bare', '--quiet', remote]);
  const token = 'test-only-screenshot-token';
  const environments = [];
  const spawn = (command, args, options) => {
    assert.equal(command, 'git');
    environments.push(options.env);
    return spawnSync(command, args, options);
  };
  const image = { relative: 'before/comet-250-portrait/tracker.png', bytes: png(1080, 1240) };
  storeImages({ images: [image], meta, remote, token, spawn });
  assert.ok(environments.length > 0);
  for (const env of environments) {
    assert.equal(env.GIT_CONFIG_COUNT, '1');
    assert.equal(env.GIT_CONFIG_KEY_0, 'http.https://github.com/.extraheader');
    assert.equal(env.GIT_CONFIG_VALUE_0,
      `AUTHORIZATION: basic ${Buffer.from(`x-access-token:${token}`).toString('base64')}`);
  }
});

test('publisher creates one comment, updates only its bot comment, and skips stale runs', async t => {
  const directory = fixture(t);
  const original = process.env.SCREENSHOT_ARTIFACT;
  process.env.SCREENSHOT_ARTIFACT = directory;
  t.after(() => {
    if (original === undefined) delete process.env.SCREENSHOT_ARTIFACT;
    else process.env.SCREENSHOT_ARTIFACT = original;
  });
  const writes = [];
  let stored = 0;
  let current = pr;
  let comments = [];
  const github = {
    rest: {
      pulls: { get: async () => ({ data: current }) },
      issues: {
        listComments: () => {},
        createComment: async value => writes.push({ kind: 'create', ...value }),
        updateComment: async value => writes.push({ kind: 'update', ...value }),
      },
    },
    paginate: async () => comments,
  };
  const core = { info: () => {}, summary: { addRaw() { return this; }, async write() {} } };
  const context = { repo: { owner: 'owner', repo: 'fitness-tracker' }, payload: { workflow_run: run } };
  const token = 'test-only-publisher-token';
  const store = ({ images, token: suppliedToken, remote }) => {
    assert.equal(suppliedToken, token);
    assert.equal(remote, 'https://github.com/owner/fitness-tracker.git');
    assert.equal(images.length, 120);
    stored++;
    return 'c'.repeat(40);
  };
  await publish({ github, context, core, token, store });
  assert.equal(writes.length, 1);
  assert.equal(writes[0].kind, 'create');
  assert.equal(writes[0].issue_number, 42);
  // A human copying the marker must not become the bot's update target.
  comments = [
    { id: 1, user: { login: 'contributor' }, body: writes[0].body },
    { id: 2, user: { login: 'github-actions[bot]' }, body: writes[0].body },
  ];
  await publish({ github, context, core, token, store });
  assert.equal(writes[1].kind, 'update');
  assert.equal(writes[1].comment_id, 2);
  current = { ...pr, state: 'closed' };
  await publish({ github, context, core, token, store });
  assert.equal(stored, 2);
  assert.equal(writes.length, 2);
  current = pr;
  await publish({ github, context, core, token, store: value => {
    const commit = store(value);
    current = { ...pr, head: { ...pr.head, sha: 'd'.repeat(40) } };
    return commit;
  } });
  assert.equal(stored, 3);
  assert.equal(writes.length, 2);
});
