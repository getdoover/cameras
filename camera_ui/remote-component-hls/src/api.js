// Basic API client for some tunnel REST actions.
// Copied from main doover repo api client.

export default class ApiClient {
  constructor() {
    this.domain = `https://${window.location.href.split('/')[2]}`;
    // this.domain = `https://dev.doover.ngrok.app`;

    this.token = null;
    // this.token = "9628a728f104e4aacb41fc80b1a127881bb5826a";
    this.token_age = null;

    this.agent_id = null;
    this.proxy_agent_id = null;
  }

  async get_temp_token() {
    return await this.get('/ch/v1/get_temp_token/', false);
  }

  async update_token() {
    await this.get_temp_token().then(response => {
      this.token = response.token;
      this.token_age = Date.now();

      if (this.agent_id == null) {
        this.agent_id = response.agent_id;
      }
    }).catch(error => {
      console.log("Error updating temp token: ", error);
      this.token = null;
      this.token_age = null;
    });
  }

  // If the token is older than 5 minutes, update it
  async ensure_token() {
    if (this.token == null || this.token_age == null || Date.now() - this.token_age > 300000) {
      await this.update_token();
    }
    if (this.token == null) {
      throw new Error("Token not available");
    }
  }

  async _request(url, method = 'GET', body = null, extraData = {}, confirm_token = true) {
    if (confirm_token) {
      await this.ensure_token();
    }

    const headers = this._constructHeaders(this.token, this.proxy_agent_id);
    const options = {
      method: method,
      headers: headers,
    };

    if (body) {
      options.body = JSON.stringify(body);
    }

    try {
      const response = await fetch(this.domain + url, options);
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      let text = await response.text();
      try {
        return JSON.parse(text);
      } catch(err) {
        return text;
      }
    } catch (error) {
      console.error(`Fetch ${method} error:`, error);
      return undefined;
    }
  }

  _constructHeaders(token, proxy_agent_id = null) {
    let headers = {
      'Content-Type': 'application/json',
      'Authorization': `Token ${token}`
    };

    if (proxy_agent_id) {
      headers['X-Proxy-Agent'] = proxy_agent_id;
    }

    return headers;
  }

  async get(url, confirm_token = true) {
    return await this._request(url, 'GET', null, {}, confirm_token);
  }

  async post(url, body, extraData = {}) {
    return await this._request(url, 'POST', body, extraData);
  }

  async getTunnelList(agentId, show_choices = true) {
    let url = `/ch/v1/agent/${agentId}/dd_tunnels/`;
    if (show_choices) {
      url += "?choices=true";
    }
    return await this.get(url);
  }

  async createTunnel(agentId, data) {
    return await this.post(`/ch/v1/agent/${agentId}/dd_tunnels/`, data);
  }

  async activateTunnel(tunnelId) {
    return await this.post(`/ch/v1/tunnels/${tunnelId}/activate/`, {});
  }

  async deactivateTunnel(tunnelId) {
    return await this.post(`/ch/v1/tunnels/${tunnelId}/deactivate/`, {});
  }

  async sendControlCommand(channel, camName, agentId, payload) {
    payload["task_id"] = window.crypto.randomUUID();
    let to_send = {[`${camName}`]: payload};
    await this.ensure_token();
    return await this._request(`/ch/v1/agent/${agentId}/${channel}/aggregate/`, "POST", to_send);
  }
}