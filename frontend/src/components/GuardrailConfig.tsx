import React from 'react';
import { useTranslation } from 'react-i18next';
import useBot from '../hooks/useBot';
import { GuardrailDisplayData as GuardRailConfigType } from '../@types/bot';
import Select from './Select';

interface GuardrailConfigProps {
  id: string;
  setGuardrailId: React.Dispatch<React.SetStateAction<string>>;
  version: string;
  setGuardrailVersion: React.Dispatch<React.SetStateAction<string>>;
  isLoading: boolean;
  errorMessages: { [label: string]: string };
}

const GuardrailConfig: React.FC<GuardrailConfigProps> = ({
  isLoading,
  ...props
}) => {
  const { t } = useTranslation();
  const { getGuardrails } = useBot();
  const [ guardrails, setGuardrails ] = React.useState<GuardRailConfigType[]>([]);
  const { setGuardrailId, setGuardrailVersion } = props;

  React.useEffect(() => {
    getGuardrails().then((response) => {
      setGuardrails(response?.guardrails ?? []);
      const defaultGuardrail = response?.guardrails?.find((guardrail) => guardrail.default);
      if (defaultGuardrail && !props.id) {
        setGuardrailId(defaultGuardrail.id);
        setGuardrailVersion(defaultGuardrail.versions[0]);
      }
    })
    .catch((error) => {
      console.error(error);
    });
  }, []);

  const guardrailsById = React.useMemo(() => {
    return guardrails.reduce((acc, guardrail) => {
      acc[guardrail.id] = {
        versions: guardrail.versions,
        id: guardrail.id,
        name: guardrail.name,
        default: guardrail.default,
      };
      return acc;
    }, {} as { [id: string]: { id: string, name: string, versions: string[], default?: boolean } });
  }, [guardrails]);

  return (
    <div>
      <div className="text-sm text-aws-font-color/50">
        {t('guardrailConfig.description')}
      </div>
      <div className="mt-2">
        <Select
          label={t('guardrailConfig.name.label')}
          disabled={isLoading}
          value={props.id}
          onChange={(id) => {props.setGuardrailId(id); props.setGuardrailVersion(guardrailsById[id]?.versions[0]);}}
          options={Object.values(guardrailsById).map(({ id, name }) => ({ value: id, label: name }))}
        />
      </div>
      <div className="mt-2">
        <Select
          label={t('guardrailConfig.version.label')}
          disabled={isLoading}
          value={props.version}
          onChange={props.setGuardrailVersion}
          options={(guardrailsById[props.id]?.versions ?? []).map((version) => ({ value: version, label: version }))}
        />
      </div>
    </div>
  );
};

export default GuardrailConfig;
