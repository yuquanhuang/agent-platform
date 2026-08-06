import {
  FetchApiTransport,
  ResourcesApiClient,
  type ApiRequest,
  type ApiTransport,
} from '@/api/generated';

class AuthenticatedApiTransport implements ApiTransport {
  constructor(
    private readonly delegate: ApiTransport,
    private readonly authorization: string | undefined,
  ) {}

  request<T>(request: ApiRequest): Promise<T> {
    return this.delegate.request<T>(this.withAuthorization(request));
  }

  openStream(request: ApiRequest): Promise<Response> {
    return this.delegate.openStream(this.withAuthorization(request));
  }

  private withAuthorization(request: ApiRequest): ApiRequest {
    if (this.authorization === undefined) return request;
    return {
      ...request,
      headers: { ...request.headers, Authorization: this.authorization },
    };
  }
}

const mockAuthorization = import.meta.env.DEV ? 'Bearer mock' : undefined;
const transport = new AuthenticatedApiTransport(new FetchApiTransport(), mockAuthorization);

export const resourcesApiClient = new ResourcesApiClient(transport);
