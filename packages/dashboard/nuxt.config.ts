import tailwindcss from '@tailwindcss/vite'

export default defineNuxtConfig({
  compatibilityDate: '2025-05-15',
  future: { compatibilityVersion: 4 },
  modules: [],

  vite: {
    plugins: [tailwindcss()],
  },

  css: ['~/assets/css/main.css'],

  devtools: { enabled: true },
  devServer: { port: 3100 },

  runtimeConfig: {
    awsRegion: process.env.AWS_REGION || 'us-east-1',
    dynamodbTableName: process.env.DYNAMODB_TABLE_NAME || 'RcaAgentDevRcaSession',
    s3ReportBucket: process.env.S3_REPORT_BUCKET || 'rca-agent-dev-evidence',
  },
})
