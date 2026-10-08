{{- define "lore.fullname" -}}
{{- .Values.nameOverride | default .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels; call with (dict "ctx" $ "component" "postgres") */}}
{{- define "lore.labels" -}}
{{ include "lore.selectorLabels" . }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
helm.sh/chart: {{ .ctx.Chart.Name }}-{{ .ctx.Chart.Version }}
{{- end -}}

{{- define "lore.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/part-of: lore
{{- end -}}

{{/* env entry reading one key from the credentials secret; call with (list $ "ENV_NAME" "secret-key") */}}
{{- define "lore.secretEnv" -}}
- name: {{ index . 1 }}
  valueFrom:
    secretKeyRef:
      name: {{ (index . 0).Values.credentialsSecret }}
      key: {{ index . 2 }}
{{- end -}}

{{/* nodeSelector/tolerations block for a component's values */}}
{{- define "lore.scheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "lore.postgresHost" -}}
{{ include "lore.fullname" . }}-postgres.{{ .Release.Namespace }}.svc.cluster.local
{{- end -}}

{{- define "lore.qdrantUrl" -}}
http://{{ include "lore.fullname" . }}-qdrant.{{ .Release.Namespace }}.svc.cluster.local:6333
{{- end -}}

{{- define "lore.embeddingsUrl" -}}
{{- if .Values.embeddings.enabled -}}
http://{{ include "lore.fullname" . }}-embeddings.{{ .Release.Namespace }}.svc.cluster.local:11434
{{- else -}}
{{- required "embeddings.url is required when embeddings.enabled is false" .Values.embeddings.url -}}
{{- end -}}
{{- end -}}
