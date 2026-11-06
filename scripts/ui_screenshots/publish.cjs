// This module runs only from the default branch, in the workflow_run publisher.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const matrix = require('./matrix.json');

const BRANCH = 'ui-screenshots';
const MARKER = '<!-- fitness-tracker-ui-screenshots -->';
const PNG_SIGNATURE = Buffer.from('89504e470d0a1a0a', 'hex');
const MAX_IMAGE_BYTES = 3 * 1024 * 1024;

function readRegularFile(root, relative, maxBytes) {
  let current = root;
  for (const component of relative.split('/')) {
    current = path.join(current, component);
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error(`Symlink in artifact: ${relative}`);
  }
  const stat = fs.statSync(current);
  if (!stat.isFile() || stat.size > maxBytes) throw new Error(`Invalid artifact file: ${relative}`);
  return fs.readFileSync(current);
}

function readMetadata(root) {
  const meta = JSON.parse(readRegularFile(root, 'metadata.json', 4096).toString('utf8'));
  if (!Number.isSafeInteger(meta.pr) || meta.pr < 1 ||
      !/^[a-f0-9]{40}$/.test(meta.base) || !/^[a-f0-9]{40}$/.test(meta.head) ||
      !/^[a-z0-9][a-z0-9-]*\/[a-z0-9_.-]+$/i.test(meta.headRepository)) {
    throw new Error('Invalid screenshot metadata');
  }
  return meta;
}

function readImages(root) {
  const images = [];
  for (const side of ['before', 'after']) {
    for (const viewport of matrix.viewports) {
      for (const page of matrix.pages) {
        // All paths and image sizes come from trusted configuration, never metadata.
        const relative = `${side}/${viewport.id}/${page.id}.png`;
        const bytes = readRegularFile(root, relative, MAX_IMAGE_BYTES);
        if (bytes.length < 45 || !bytes.subarray(0, 8).equals(PNG_SIGNATURE) ||
            bytes.readUInt32BE(8) !== 13 || bytes.toString('ascii', 12, 16) !== 'IHDR' ||
            bytes.readUInt32BE(16) !== Math.round(viewport.width * viewport.scale) ||
            bytes.readUInt32BE(20) !== Math.round(viewport.height * viewport.scale) ||
            bytes.toString('ascii', bytes.length - 8, bytes.length - 4) !== 'IEND') {
          throw new Error(`Invalid PNG or dimensions: ${relative}`);
        }
        images.push({ relative, bytes });
      }
    }
  }
  return images;
}

function currentPullRequest(run, meta, pr, repository) {
  if (run.event !== 'pull_request' || run.conclusion !== 'success' ||
      run.path?.split('@')[0] !== '.github/workflows/ui-screenshots.yml' ||
      run.repository.full_name !== repository || run.head_sha !== meta.head) {
    throw new Error('Screenshot artifact does not match the triggering PR workflow');
  }
  if (run.pull_requests?.length && !run.pull_requests.some(item => item.number === meta.pr)) {
    throw new Error('Screenshot artifact targets an unrelated PR');
  }
  // Fork workflow_run payloads may have no pull_requests. Resolve via the metadata
  // number, then require the live PR's exact repository, branch and revision.
  return pr.state === 'open' && pr.number === meta.pr &&
    pr.base.repo.full_name === repository && pr.base.sha === meta.base &&
    pr.head.repo?.full_name === meta.headRepository && pr.head.sha === meta.head &&
    pr.head.ref === run.head_branch && run.head_repository?.full_name === meta.headRepository;
}

function imageComment(meta, commit, repository, runUrl) {
  const root = `https://raw.githubusercontent.com/${repository}/${commit}/pr-${meta.pr}`;
  const lines = [
    MARKER,
    '## UI screenshots',
    '',
    `Before: \`${meta.base.slice(0, 12)}\` · After: \`${meta.head.slice(0, 12)}\` · [CI run and full-resolution artifact](${runUrl})`,
    '',
    'Six pages at ten layouts. Expand a device to compare the base and PR head.',
    'Dark theme, fixed sample activities and live metrics; viewport captures include application navigation.',
    '',
  ];
  for (const viewport of matrix.viewports) {
    lines.push('<details>', `<summary>${viewport.label} — ${viewport.width}×${viewport.height} logical pixels</summary>`, '');
    lines.push('| Page | Before | After |', '| --- | --- | --- |');
    for (const page of matrix.pages) {
      const image = side => `<img src="${root}/${side}/${viewport.id}/${page.id}.png" width="320" alt="${page.label}, ${viewport.label}, ${side}">`;
      lines.push(`| ${page.label} | ${image('before')} | ${image('after')} |`);
    }
    lines.push('', '</details>', '');
  }
  return lines.join('\n');
}

function storeImages({ images, meta, remote, token }) {
  // Use a fresh object database, with no checkout, hooks or executable files from
  // either the PR artifact or the image branch. Push only allowlisted PNG blobs.
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'fitness-ui-images-'));
  const env = {
    ...process.env,
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_CONFIG_GLOBAL: '/dev/null',
    GIT_TERMINAL_PROMPT: '0',
    GIT_AUTHOR_NAME: 'github-actions[bot]',
    GIT_AUTHOR_EMAIL: '41898282+github-actions[bot]@users.noreply.github.com',
    GIT_COMMITTER_NAME: 'github-actions[bot]',
    GIT_COMMITTER_EMAIL: '41898282+github-actions[bot]@users.noreply.github.com',
  };
  if (token) {
    env.GIT_CONFIG_COUNT = '1';
    env.GIT_CONFIG_KEY_0 = 'http.https://github.com/.extraheader';
    env.GIT_CONFIG_VALUE_0 = `AUTHORIZATION: basic ${Buffer.from(`x-access-token:${token}`).toString('base64')}`;
  }
  const git = (args, input, allowFailure = false) => {
    const result = spawnSync('git', ['-c', 'core.hooksPath=/dev/null', ...args], {
      cwd: directory, env, input, encoding: 'utf8', timeout: 120000,
    });
    if (result.error || (result.status !== 0 && !allowFailure)) {
      throw new Error(`git ${args[0]} failed: ${result.error?.message || result.stderr}`);
    }
    return result;
  };
  try {
    git(['init', '--quiet']);
    git(['remote', 'add', 'origin', remote]);
    const exists = git(['ls-remote', '--exit-code', '--heads', 'origin', BRANCH], undefined, true);
    if (![0, 2].includes(exists.status)) throw new Error('Cannot inspect screenshot branch');
    let parent;
    if (exists.status === 0) {
      git(['fetch', '--quiet', '--depth=1', 'origin', `refs/heads/${BRANCH}`]);
      parent = git(['rev-parse', 'FETCH_HEAD']).stdout.trim();
      git(['read-tree', parent]);
    }
    for (const image of images) {
      const sha = git(['hash-object', '-w', '--stdin'], image.bytes).stdout.trim();
      git(['update-index', '--add', '--cacheinfo', '100644', sha, `pr-${meta.pr}/${image.relative}`]);
    }
    const tree = git(['write-tree']).stdout.trim();
    const commit = git(
      ['commit-tree', tree, ...(parent ? ['-p', parent] : [])],
      `UI screenshots for PR #${meta.pr} at ${meta.head}\n`,
    ).stdout.trim();
    git(['push', '--quiet', 'origin', `${commit}:refs/heads/${BRANCH}`]);
    return commit;
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function publish({ github, context, core, token, store = storeImages }) {
  const run = context.payload.workflow_run;
  const repository = `${context.repo.owner}/${context.repo.repo}`;
  const root = process.env.SCREENSHOT_ARTIFACT;
  const meta = readMetadata(root);
  const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: meta.pr });
  if (!currentPullRequest(run, meta, pr, repository)) {
    core.info('Skipping screenshots for a closed PR or superseded base/head revision.');
    return;
  }
  const images = readImages(root);
  const commit = store({
    images, meta, remote: `https://github.com/${repository}.git`, token,
  });
  // A push may have arrived while images were uploaded. Do not replace a newer report.
  const { data: latest } = await github.rest.pulls.get({ ...context.repo, pull_number: meta.pr });
  if (!currentPullRequest(run, meta, latest, repository)) {
    core.info('PR changed during publication; leaving its comment untouched.');
    return;
  }
  const body = imageComment(meta, commit, repository, run.html_url);
  const comments = await github.paginate(github.rest.issues.listComments, {
    ...context.repo, issue_number: meta.pr, per_page: 100,
  });
  const previous = comments.find(comment =>
    comment.user?.login === 'github-actions[bot]' && comment.body?.startsWith(MARKER));
  if (previous) {
    await github.rest.issues.updateComment({ ...context.repo, comment_id: previous.id, body });
  } else {
    await github.rest.issues.createComment({ ...context.repo, issue_number: meta.pr, body });
  }
  await core.summary.addRaw(body).write();
}

module.exports = { currentPullRequest, imageComment, publish, readImages, readMetadata, storeImages };
