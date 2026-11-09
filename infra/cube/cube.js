// Nova's Cube configuration.
//
// Nova's API is Cube's only client. For every request it signs a short-lived token with
// CUBEJS_API_SECRET that says who is asking and what they may read, and Cube is only reachable
// on the Compose network. Everything here fails closed: a query is answered only if every cube it
// touches is listed in an access manifest Nova wrote, in a catalog the user may read, and, for
// someone limited to some tenants, either carries the tenant dimension or holds shared data.
//
// The manifests are written by Nova's code, never by a model, from the sources' tenancy settings.
// The data models themselves are drafted by a model and checked by Nova before they're published
// here: see agent/semantic/validate.py.

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const jwt = require('jsonwebtoken');

const SKILLS = process.env.NOVA_SKILLS_DIR || '/nova/skills';
const MAX_ROWS = Number(process.env.NOVA_CUBE_MAX_ROWS || 50000);
const LIVE = 'live';
const DRAFT = /^draft-[a-z0-9]{8,32}$/;
const GRANULARITIES = new Set(['second', 'minute', 'hour', 'day', 'week', 'month', 'quarter', 'year']);

/** The directories holding one model's files: every published skill, or one draft. */
function modelDirs(model) {
  if (model === LIVE) {
    return fs.readdirSync(SKILLS, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && !entry.name.startsWith('_') && !entry.name.startsWith('.'))
      .map((entry) => path.join(SKILLS, entry.name, 'model'))
      .filter((dir) => fs.existsSync(dir));
  }
  if (DRAFT.test(model)) {
    const dir = path.join(SKILLS, '_drafts', model.slice('draft-'.length), 'model');
    return fs.existsSync(dir) ? [dir] : [];
  }
  throw new Error('unknown data model');
}

function filesIn(dir, extension) {
  return fs.readdirSync(dir).filter((name) => name.endsWith(extension)).sort().map((name) => path.join(dir, name));
}

function modelOf(securityContext) {
  const model = (securityContext && securityContext.model) || LIVE;
  if (model !== LIVE && !DRAFT.test(model)) {
    throw new Error('unknown data model');
  }
  return model;
}

/** cube name -> { catalog, tenant_dimension, shared }, from every manifest in the model. */
function manifestOf(model) {
  const cubes = new Map();
  for (const dir of modelDirs(model)) {
    const file = path.join(dir, 'access.json');
    if (!fs.existsSync(file)) {
      continue; // A model without a manifest is unreadable: none of its cubes are listed.
    }
    const manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const [name, rule] of Object.entries(manifest.cubes || {})) {
      if (cubes.has(name)) {
        throw new Error(`cube ${name} is defined by two sources`);
      }
      cubes.set(name, { catalog: manifest.catalog, tenant_dimension: rule.tenant_dimension || null, shared: !!rule.shared });
    }
  }
  return cubes;
}

/** Every member name a query refers to, wherever it appears. */
function membersOf(query) {
  const found = [];
  const add = (member) => found.push(member);
  (query.measures || []).forEach(add);
  (query.dimensions || []).forEach(add);
  (query.segments || []).forEach(add);
  (query.timeDimensions || []).forEach((item) => add(item.dimension));
  const walk = (filters) => (filters || []).forEach((filter) => {
    if (filter.and || filter.or) {
      walk(filter.and || filter.or);
    } else {
      add(filter.member !== undefined ? filter.member : filter.dimension);
    }
  });
  walk(query.filters);
  const order = query.order || [];
  (Array.isArray(order) ? order.map((item) => (Array.isArray(item) ? item[0] : item.id)) : Object.keys(order)).forEach(add);
  return found;
}

/** The cube a member belongs to. Anything other than cube.member, or cube.time.granularity,
 *  is refused: a longer path walks through joins, and its rows couldn't be held to a rule. */
function cubeOf(member, cubes) {
  if (typeof member !== 'string') {
    throw new Error('this query uses a member expression, which is not available');
  }
  const parts = member.split('.');
  if (parts.length === 2 || (parts.length === 3 && GRANULARITIES.has(parts[2]) && !cubes.has(parts[1]))) {
    return parts[0];
  }
  throw new Error(`${member} is not a member Nova can answer for`);
}

function checkContext(securityContext) {
  const ok = securityContext && typeof securityContext.sub === 'string' && Array.isArray(securityContext.catalogs)
    && securityContext.scopes && typeof securityContext.scopes === 'object';
  if (!ok) {
    throw new Error('this request carries no usable identity');
  }
}

module.exports = {
  // Signed by Nova's API, HS256 only, and short-lived. Anything else is refused.
  checkAuth: async (req, auth) => {
    if (!auth) {
      throw new Error('a signed token is required');
    }
    const token = auth.startsWith('Bearer ') ? auth.slice('Bearer '.length) : auth;
    const claims = jwt.verify(token, process.env.CUBEJS_API_SECRET, { algorithms: ['HS256'], requireExp: true });
    if (!claims.exp) {
      throw new Error('the token has no expiry');
    }
    checkContext(claims);
    req.securityContext = claims;
  },

  // GraphQL and jobs aren't used, so they're closed.
  contextToApiScopes: async () => ['meta', 'data', 'sql'],

  contextToAppId: ({ securityContext }) => `NOVA_${modelOf(securityContext)}`,

  repositoryFactory: ({ securityContext }) => {
    const model = modelOf(securityContext);
    return {
      dataSchemaFiles: async () => modelDirs(model).flatMap((dir) => filesIn(dir, '.yml').map((file) => ({
        fileName: path.relative(SKILLS, file),
        content: fs.readFileSync(file, 'utf8'),
      }))),
    };
  },

  // A change to any model file recompiles the model on the next request, without a restart.
  schemaVersion: ({ securityContext }) => {
    const hash = crypto.createHash('sha256');
    for (const dir of modelDirs(modelOf(securityContext))) {
      for (const file of filesIn(dir, '.yml')) {
        const stat = fs.statSync(file);
        hash.update(`${file}:${stat.size}:${stat.mtimeMs};`);
      }
    }
    return hash.digest('hex');
  },

  queryRewrite: (query, { securityContext }) => {
    checkContext(securityContext);
    const cubes = manifestOf(modelOf(securityContext));
    const touched = new Set(membersOf(query).map((member) => cubeOf(member, cubes)));
    if (touched.size === 0) {
      throw new Error('the query names no members');
    }
    const filters = [];
    for (const name of touched) {
      const rule = cubes.get(name);
      if (!rule || !securityContext.catalogs.includes(rule.catalog)) {
        throw new Error(`${name} isn't available to you`);
      }
      const tenants = securityContext.scopes[rule.catalog];
      if (tenants === undefined || tenants === null) {
        continue; // Not limited on this source.
      }
      if (!Array.isArray(tenants) || tenants.length === 0) {
        throw new Error(`you have no tenants on ${rule.catalog}`);
      }
      if (rule.tenant_dimension) {
        filters.push({ member: `${name}.${rule.tenant_dimension}`, operator: 'equals', values: tenants.map(String) });
      } else if (!rule.shared) {
        throw new Error(`${name} can't be limited to your data, so it isn't available to you`);
      }
    }
    query.filters = [...(query.filters || []), ...filters];
    query.limit = Math.min(Number(query.limit) || MAX_ROWS, MAX_ROWS);
    return query;
  },

  // Nothing is pre-aggregated, and a scheduled refresh would run with no identity.
  scheduledRefreshTimer: false,
  telemetry: false,
};
