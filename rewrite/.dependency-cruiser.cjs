const entrypoints = '^src/(?:main|import|observer-login)\\.ts$'
const telegramPackages =
  '(?:^|/)node_modules/(?:grammy|telegram)(?:/|$)|^(?:grammy|telegram)(?:/|$)'
const grammy = '(?:^|/)node_modules/grammy(?:/|$)|^grammy(?:/|$)'

module.exports = {
  forbidden: [
    { name: 'no-circular', severity: 'error', from: {}, to: { circular: true } },
    { name: 'no-unresolved', severity: 'error', from: {}, to: { couldNotResolve: true } },
    {
      name: 'no-orphans',
      severity: 'error',
      from: { path: entrypoints },
      to: { path: '^src/', pathNot: entrypoints, reachable: false },
    },
    {
      name: 'domain-is-pure',
      severity: 'error',
      from: { path: '^src/domain\\.ts$' },
      to: {
        path: [
          '^src/(?:storage|classification|runtime)/',
          '^src/(?:telegram[^/]*|observer[^/]*|commands|engine|store)\\.ts$',
          telegramPackages,
          '^bun:sqlite$',
        ],
      },
    },
    {
      name: 'storage-is-independent',
      severity: 'error',
      from: { path: '^src/storage/' },
      to: {
        path: [
          '^src/(?:classification|runtime)/',
          '^src/(?:classifier|telegram|observer[^/]*|engine|commands|application)\\.ts$',
          telegramPackages,
        ],
      },
    },
    {
      name: 'observation-is-read-only',
      severity: 'error',
      from: { path: '^src/(?:observer[^/]*|media)\\.ts$' },
      to: {
        path: ['^src/(?:telegram|engine|commands|storage/actions)\\.ts$', grammy],
      },
    },
    {
      name: 'classification-is-independent',
      severity: 'error',
      from: { path: '^src/(?:classification/|classifier\\.ts$)' },
      to: {
        path: ['^src/(?:storage|runtime)/', '^src/(?:store|engine|observer[^/]*|commands)\\.ts$'],
      },
    },
    {
      name: 'commands-are-independent',
      severity: 'error',
      from: { path: '^src/commands\\.ts$' },
      to: {
        path: [
          '^src/classification/',
          '^src/(?:engine|classifier|observer[^/]*|application)\\.ts$',
        ],
      },
    },
    {
      name: 'engine-is-independent',
      severity: 'error',
      from: { path: '^src/engine\\.ts$' },
      to: {
        path: [
          '^src/(?:runtime|classification)/',
          '^src/(?:application|observer[^/]*|commands|telegram)\\.ts$',
        ],
      },
    },
  ],
  options: {
    doNotFollow: { path: 'node_modules' },
    tsConfig: { fileName: 'tsconfig.json' },
    tsPreCompilationDeps: true,
    enhancedResolveOptions: {
      exportsFields: ['exports'],
      conditionNames: ['bun', 'import', 'default'],
    },
  },
}
