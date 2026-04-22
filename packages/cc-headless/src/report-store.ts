import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

const S3_REPORT_BUCKET = process.env.S3_REPORT_BUCKET ?? '';
const SNS_TOPIC_ARN = process.env.SNS_NOTIFICATION_TOPIC_ARN ?? '';
const PRESIGNED_URL_EXPIRY = 86400; // 24 hours

const s3 = new S3Client({});
const sns = new SNSClient({});

export async function saveReport(
  rcaId: string,
  reportMarkdown: string,
): Promise<string> {
  const key = `reports/${rcaId}.md`;
  if (!S3_REPORT_BUCKET) return key;

  await s3.send(
    new PutObjectCommand({
      Bucket: S3_REPORT_BUCKET,
      Key: key,
      Body: reportMarkdown,
      ContentType: 'text/markdown',
    }),
  );

  return key;
}

export async function sendNotification(
  rcaId: string,
  alarmName: string,
  rootCause: string,
  reportS3Key: string,
  elapsedSeconds: number,
): Promise<void> {
  if (!SNS_TOPIC_ARN) return;

  let reportUrl = `s3://${S3_REPORT_BUCKET}/${reportS3Key}`;
  if (S3_REPORT_BUCKET) {
    try {
      reportUrl = await getSignedUrl(
        s3,
        new GetObjectCommand({
          Bucket: S3_REPORT_BUCKET,
          Key: reportS3Key,
        }),
        { expiresIn: PRESIGNED_URL_EXPIRY },
      );
    } catch {
      // fall back to S3 URI
    }
  }

  const message = JSON.stringify({
    rca_id: rcaId,
    alarm_name: alarmName,
    root_cause: rootCause,
    report_url: reportUrl,
    engine: 'cc-headless',
    elapsed_seconds: elapsedSeconds,
  });

  await sns.send(
    new PublishCommand({
      TopicArn: SNS_TOPIC_ARN,
      Subject: `[RCA] ${alarmName} — Analysis Complete (cc-headless)`,
      Message: message,
    }),
  );
}
