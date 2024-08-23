#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { BedrockChatStack } from "../lib/bedrock-chat-stack";
import { TIdentityProvider } from "../lib/utils/identity-provider";
import { CronScheduleProps } from "../lib/utils/cron-schedule";

const app = new cdk.App();

const BEDROCK_REGION = app.node.tryGetContext("bedrockRegion");

// Allowed IP address ranges for this app itself
const ALLOWED_IP_V4_ADDRESS_RANGES: string[] = app.node.tryGetContext(
  "allowedIpV4AddressRanges"
);
const ALLOWED_IP_V6_ADDRESS_RANGES: string[] = app.node.tryGetContext(
  "allowedIpV6AddressRanges"
);

// Allowed IP address ranges for the published API
const PUBLISHED_API_ALLOWED_IP_V4_ADDRESS_RANGES: string[] =
  app.node.tryGetContext("publishedApiAllowedIpV4AddressRanges");
const PUBLISHED_API_ALLOWED_IP_V6_ADDRESS_RANGES: string[] =
  app.node.tryGetContext("publishedApiAllowedIpV6AddressRanges");
const ALLOWED_SIGN_UP_EMAIL_DOMAINS: string[] = app.node.tryGetContext(
  "allowedSignUpEmailDomains"
);
const IDENTITY_PROVIDERS: TIdentityProvider[] =
  app.node.tryGetContext("identityProviders");
const USER_POOL_DOMAIN_PREFIX: string = app.node.tryGetContext(
  "userPoolDomainPrefix"
);
const AUTO_JOIN_USER_GROUPS: string[] =
  app.node.tryGetContext("autoJoinUserGroups");

const RDS_SCHEDULES: CronScheduleProps = app.node.tryGetContext("rdbSchedules");
const ENABLE_MISTRAL: boolean = app.node.tryGetContext("enableMistral");
const SELF_SIGN_UP_ENABLED: boolean =
  app.node.tryGetContext("selfSignUpEnabled");

// container size of embedding ecs tasks
const EMBEDDING_CONTAINER_VCPU: number = app.node.tryGetContext(
  "embeddingContainerVcpu"
);
const EMBEDDING_CONTAINER_MEMORY: number = app.node.tryGetContext(
  "embeddingContainerMemory"
);

// how many nat gateways
const NATGATEWAY_COUNT: number = app.node.tryGetContext("natgatewayCount");

const chat = new BedrockChatStack(app, `AishaBedrockChatStack`, {
  env: {
    // account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  crossRegionReferences: true,
  bedrockRegion: BEDROCK_REGION,
  identityProviders: IDENTITY_PROVIDERS,
  userPoolDomainPrefix: USER_POOL_DOMAIN_PREFIX,
  publishedApiAllowedIpV4AddressRanges:
    PUBLISHED_API_ALLOWED_IP_V4_ADDRESS_RANGES,
  publishedApiAllowedIpV6AddressRanges:
    PUBLISHED_API_ALLOWED_IP_V6_ADDRESS_RANGES,
  allowedSignUpEmailDomains: ALLOWED_SIGN_UP_EMAIL_DOMAINS,
  autoJoinUserGroups: AUTO_JOIN_USER_GROUPS,
  rdsSchedules: RDS_SCHEDULES,
  enableMistral: ENABLE_MISTRAL,
  embeddingContainerVcpu: EMBEDDING_CONTAINER_VCPU,
  embeddingContainerMemory: EMBEDDING_CONTAINER_MEMORY,
  selfSignUpEnabled: SELF_SIGN_UP_ENABLED,
  natgatewayCount: NATGATEWAY_COUNT,
});
