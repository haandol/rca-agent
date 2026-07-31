import tailwindcss from '@tailwindcss/vite';

export default defineNuxtConfig({
  compatibilityDate: '2025-05-15',
  future: { compatibilityVersion: 4 },
  modules: [],

  vite: {
    plugins: [tailwindcss()],
  },

  css: ['~/assets/css/main.css'],

  // The floating devtools button sits over the bottom-right of every page,
  // which is where session rows and execution history end up on long lists.
  devtools: { enabled: false },
  devServer: { port: 3100 },

  runtimeConfig: {
    awsRegion: process.env.AWS_REGION || 'us-east-1',
    dynamodbTableName:
      process.env.DYNAMODB_TABLE_NAME || 'RcaAgentDevRcaSession',
    s3ReportBucket: process.env.S3_REPORT_BUCKET || 'rca-agent-dev-evidence',
    // Publishing here is what approves an execution, so it has no default: a
    // misconfigured dashboard must fail to approve rather than approve elsewhere.
    executionQueueUrl: process.env.EXECUTION_QUEUE_URL || '',
  },
});
