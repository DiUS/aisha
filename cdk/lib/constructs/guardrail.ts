// The code below shows an example of how to instantiate this type.
// The values are placeholders you should change.
import { aws_bedrock as bedrock, aws_kms as kms } from 'aws-cdk-lib';
import { Construct } from "constructs";

export class Guardrail extends Construct {
  readonly guardrail: bedrock.CfnGuardrail;
  readonly version: bedrock.CfnGuardrailVersion;

  constructor(scope: Construct, id: string, props: bedrock.CfnGuardrailProps) {
    super(scope, id);

    const cfnGuardrail = new bedrock.CfnGuardrail(this, "Guardrail", {
      ...props,
    });

    const version = new bedrock.CfnGuardrailVersion(this, "GuardrailVersion", {
      guardrailIdentifier: cfnGuardrail.attrGuardrailId,
    });

    version.addDependency(cfnGuardrail);

    this.guardrail = cfnGuardrail;
    this.version = version;
  }
}