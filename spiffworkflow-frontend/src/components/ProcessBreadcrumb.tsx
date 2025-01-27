// @ts-ignore
import { Breadcrumb, BreadcrumbItem } from '@carbon/react';
import { useEffect, useState } from 'react';
import { modifyProcessIdentifierForPathParam } from '../helpers';
import {
  HotCrumbItem,
  HotCrumbItemArray,
  HotCrumbItemObject,
  ProcessGroup,
  ProcessGroupLite,
  ProcessModel,
} from '../interfaces';
import HttpService from '../services/HttpService';

// it is recommend to use a state for hotCrumbs so ProcessBreadCrumb does not attmept
// to re-render. This is because javascript cannot tell if an array or object has changed
// but react states can. If we simply initialize a ProcessBreadCrumb when
// the component that uses it renders, we may get a request to process model show
// every time a state changes in the parent component (any state, not even a related state).
// For an example of usage see TaskShow.
type OwnProps = {
  hotCrumbs?: HotCrumbItem[];
};

export default function ProcessBreadcrumb({ hotCrumbs }: OwnProps) {
  const [processEntity, setProcessEntity] = useState<
    ProcessGroup | ProcessModel | null
  >(null);

  useEffect(() => {
    const explodeCrumbItemObject = (crumb: HotCrumbItem) => {
      if ('entityToExplode' in crumb) {
        const { entityToExplode, entityType } = crumb;
        if (entityType === 'process-model-id') {
          HttpService.makeCallToBackend({
            path: `/process-models/${modifyProcessIdentifierForPathParam(
              entityToExplode as string
            )}`,
            successCallback: setProcessEntity,
            onUnauthorized: () => {},
          });
        } else if (entityType === 'process-group-id') {
          HttpService.makeCallToBackend({
            path: `/process-groups/${modifyProcessIdentifierForPathParam(
              entityToExplode as string
            )}`,
            successCallback: setProcessEntity,
            onUnauthorized: () => {},
          });
        } else {
          setProcessEntity(entityToExplode as any);
        }
      }
    };
    if (hotCrumbs) {
      hotCrumbs.forEach(explodeCrumbItemObject);
    }
  }, [setProcessEntity, hotCrumbs]);

  const checkPermissions = (crumb: HotCrumbItemObject) => {
    if (!crumb.checkPermission) {
      return true;
    }
    return (
      processEntity &&
      'actions' in processEntity &&
      processEntity.actions &&
      'read' in processEntity.actions
    );
  };

  // eslint-disable-next-line sonarjs/cognitive-complexity
  const hotCrumbElement = () => {
    if (hotCrumbs) {
      const leadingCrumbLinks = hotCrumbs.map(
        (crumb: HotCrumbItemArray | HotCrumbItemObject) => {
          if (
            'entityToExplode' in crumb &&
            processEntity &&
            processEntity.parent_groups &&
            checkPermissions(crumb)
          ) {
            const breadcrumbs = processEntity.parent_groups.map(
              (parentGroup: ProcessGroupLite) => {
                const fullUrl = `/process-groups/${modifyProcessIdentifierForPathParam(
                  parentGroup.id
                )}`;
                return (
                  <BreadcrumbItem key={parentGroup.id} href={fullUrl}>
                    {parentGroup.display_name}
                  </BreadcrumbItem>
                );
              }
            );

            if (crumb.linkLastItem) {
              let apiBase = '/process-groups';
              let dataQaTag = '';
              if (crumb.entityType.startsWith('process-model')) {
                apiBase = '/process-models';
                dataQaTag = 'process-model-breadcrumb-link';
              }
              const fullUrl = `${apiBase}/${modifyProcessIdentifierForPathParam(
                processEntity.id
              )}`;
              breadcrumbs.push(
                <BreadcrumbItem
                  key={processEntity.id}
                  href={fullUrl}
                  data-qa={dataQaTag}
                >
                  {processEntity.display_name}
                </BreadcrumbItem>
              );
            } else {
              breadcrumbs.push(
                <BreadcrumbItem key={processEntity.id} isCurrentPage>
                  {processEntity.display_name}
                </BreadcrumbItem>
              );
            }
            return breadcrumbs;
          }
          if (Array.isArray(crumb)) {
            const valueLabel = crumb[0];
            const url = crumb[1];
            if (!url && valueLabel) {
              return (
                <BreadcrumbItem isCurrentPage key={valueLabel}>
                  {valueLabel}
                </BreadcrumbItem>
              );
            }
            if (url && valueLabel) {
              return (
                <BreadcrumbItem key={valueLabel} href={url}>
                  {valueLabel}
                </BreadcrumbItem>
              );
            }
          }
          return null;
        }
      );
      return <Breadcrumb noTrailingSlash>{leadingCrumbLinks}</Breadcrumb>;
    }
    return null;
  };

  return <Breadcrumb noTrailingSlash>{hotCrumbElement()}</Breadcrumb>;
}
