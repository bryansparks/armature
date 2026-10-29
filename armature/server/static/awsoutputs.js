// Fills the AWS connection form from pasted CloudFormation stack outputs. deploy/aws/README.md lists the mapping.

export const OUTPUT_FIELDS = {
  HostManagerRoleArn: 'role_arn',
  SessionRoleArn: 'session_role_arn',
  AllowHostAllocation: 'allow_host_allocation',
  SecurityGroupId: 'security_group_id',
  InstanceProfileName: 'instance_profile',
  LaunchTemplateIds: 'template_linux',
  BucketName: 'artifact_bucket',
  MacLaunchTemplateId: 'template_mac',
  MacInstanceProfileName: 'mac_instance_profile',
  ImageRepositoryUri: 'image_repository_uri',
};

const OBJECT = /\{[^{}]*\}/g;
const JSON_KEY = /"OutputKey"\s*:\s*"([^"]*)"/;
const JSON_VALUE = /"OutputValue"\s*:\s*"([^"]*)"/;
const LINE = /^\s*"?([A-Za-z][A-Za-z0-9]*)"?\s*[=:\t ]\s*"?([^"\s,]+)"?,?\s*$/;

// Takes the JSON of describe-stacks, its Outputs list, a {key: value} object, or KEY=VALUE, KEY: VALUE, or
// tab-separated lines. Several stacks pasted one after another work too.
export function parseOutputs(text) {
  const found = {};
  const source = String(text || '');
  for (const block of source.match(OBJECT) || []) {
    const key = block.match(JSON_KEY);
    const value = block.match(JSON_VALUE);
    if (key && value) found[key[1]] = value[1];
  }
  if (Object.keys(found).length) return found;
  try {
    const parsed = JSON.parse(source);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const [key, value] of Object.entries(parsed)) if (typeof value === 'string') found[key] = value;
      return found;
    }
  } catch {}
  for (const line of source.split('\n')) {
    const match = line.match(LINE);
    if (match) found[match[1]] = match[2];
  }
  return found;
}

// Only the outputs the connection form uses, renamed to its field names. Others are ignored.
export function connectionValues(outputs) {
  const values = {};
  for (const [output, field] of Object.entries(OUTPUT_FIELDS)) {
    if (!(output in outputs)) continue;
    values[field] = field === 'allow_host_allocation' ? String(outputs[output]).toLowerCase() === 'true' : String(outputs[output]).trim();
  }
  return values;
}
