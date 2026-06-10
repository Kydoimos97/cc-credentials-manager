import Anthropic from "@anthropic-ai/sdk";

/**
 * Credential Provider: Manages credential retrieval and lifecycle.
 *
 * Supports multiple sources:
 * - Environment variables (with optional namespace prefix)
 * - File-based storage (JSON or plain text)
 * - Direct in-memory storage (for testing or config)
 *
 * Implements lazy loading and optional caching to reduce I/O overhead.
 */

export interface CredentialProviderConfig {
  namespace?: string;
  cacheTtlMs?: number;
  filePrefix?: string;
  projectDir?: string;
}

export interface StoredCredential {
  id: string;
  value: string;
  createdAt: number;
  expiresAt?: number;
  metadata?: Record<string, unknown>;
}

export class CredentialProvider {
  private config: CredentialProviderConfig;
  private cache: Map<string, { value: StoredCredential; expiresAt: number }>;
  private memoryStore: Map<string, StoredCredential>;

  constructor(config: CredentialProviderConfig = {}) {
    this.config = {
      cacheTtlMs: 3600000, // 1 hour default
      ...config,
    };
    this.cache = new Map();
    this.memoryStore = new Map();
  }

  /**
   * Retrieves a credential by ID from the configured sources.
   * Search order: memory store → cache → environment → file
   */
  async get(credentialId: string): Promise<StoredCredential | null> {
    // Check memory store first (immediate hits)
    const memStored = this.memoryStore.get(credentialId);
    if (memStored) {
      if (!memStored.expiresAt || memStored.expiresAt > Date.now()) {
        return memStored;
      }
      this.memoryStore.delete(credentialId);
    }

    // Check cache
    const cached = this.cache.get(credentialId);
    if (cached && cached.expiresAt > Date.now()) {
      return cached.value;
    }
    this.cache.delete(credentialId);

    // Check environment
    const envValue = this.getFromEnvironment(credentialId);
    if (envValue) {
      const credential: StoredCredential = {
        id: credentialId,
        value: envValue,
        createdAt: Date.now(),
      };
      this.setCacheEntry(credentialId, credential);
      return credential;
    }

    // Check file (if projectDir is set)
    if (this.config.projectDir) {
      const fileCredential = await this.getFromFile(credentialId);
      if (fileCredential) {
        this.setCacheEntry(credentialId, fileCredential);
        return fileCredential;
      }
    }

    return null;
  }

  /**
   * Stores a credential in memory (volatile storage for testing/config).
   */
  set(credentialId: string, value: string, expiresAt?: number): void {
    const credential: StoredCredential = {
      id: credentialId,
      value,
      createdAt: Date.now(),
      expiresAt,
    };
    this.memoryStore.set(credentialId, credential);
  }

  /**
   * Clears a credential from all caches and memory.
   */
  clear(credentialId: string): void {
    this.memoryStore.delete(credentialId);
    this.cache.delete(credentialId);
  }

  /**
   * Clears all stored credentials.
   */
  clearAll(): void {
    this.memoryStore.clear();
    this.cache.clear();
  }

  /**
   * Retrieves a credential from environment variables.
   * If namespace is set, prepends it with underscore: `{namespace}_{credentialId}`
   */
  private getFromEnvironment(credentialId: string): string | undefined {
    const envKey = this.config.namespace
      ? `${this.config.namespace}_${credentialId}`
      : credentialId;
    return process.env[envKey];
  }

  /**
   * Retrieves a credential from file storage.
   * Placeholder for file-based retrieval logic (not implemented in this version).
   */
  private async getFromFile(credentialId: string): Promise<StoredCredential | null> {
    // Placeholder: implement file-based retrieval if needed
    // This would read from JSON or text files in config.projectDir
    return null;
  }

  /**
   * Stores a credential in the cache with TTL.
   */
  private setCacheEntry(credentialId: string, credential: StoredCredential): void {
    const expiresAt = Date.now() + (this.config.cacheTtlMs || 3600000);
    this.cache.set(credentialId, { value: credential, expiresAt });
  }

  /**
   * Utility: Retrieve a credential and decode it using the provided codec.
   * Used by Claude API integration to ensure consistent encoding/decoding.
   */
  async getAndDecode<T>(
    credentialId: string,
    decoder: (value: string) => T
  ): Promise<T | null> {
    const credential = await this.get(credentialId);
    if (!credential) return null;
    return decoder(credential.value);
  }
}

/**
 * Factory function to create a CredentialProvider configured for Anthropic SDK usage.
 */
export function createCredentialProvider(
  config: CredentialProviderConfig = {}
): CredentialProvider {
  return new CredentialProvider({
    namespace: "ANTHROPIC",
    ...config,
  });
}
