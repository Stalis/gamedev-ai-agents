using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace GamedevAgentKit.Editor
{
    internal static class AgentKitProcess
    {
        internal const int PythonNotFoundExitCode = -9001;

        private static Process _runningProcess;
        private static Action<int> _runningOnExit;
        private static EditorApplication.CallbackFunction _runningPoll;

        static AgentKitProcess()
        {
            AssemblyReloadEvents.beforeAssemblyReload += StopRunningProcess;
        }

        internal static void RunPythonAsync(string scriptRelPath, string workingDir, Action<int> onExit)
        {
            RunPythonAsync(scriptRelPath, workingDir, null, onExit);
        }

        internal static void RunPythonAsync(string scriptRelPath, string workingDir, IEnumerable<string> scriptArguments, Action<int> onExit)
        {
            if (string.IsNullOrEmpty(workingDir) || string.IsNullOrEmpty(scriptRelPath))
            {
                onExit?.Invoke(PythonNotFoundExitCode);
                return;
            }

            StopRunningProcess();

            var absoluteScriptPath = Path.IsPathRooted(scriptRelPath)
                ? scriptRelPath
                : Path.Combine(workingDir, scriptRelPath.Replace('/', Path.DirectorySeparatorChar));
            absoluteScriptPath = Path.GetFullPath(absoluteScriptPath);

            Process process = null;
            foreach (var interpreter in PythonExecutable.Candidates())
            {
                try
                {
                    process = Start(interpreter, absoluteScriptPath, workingDir, scriptArguments);
                    break;
                }
                catch (Win32Exception)
                {
                    // Not installed under this name; try the next candidate.
                }
                catch (Exception)
                {
                    onExit?.Invoke(PythonNotFoundExitCode);
                    return;
                }
            }

            if (process == null)
            {
                onExit?.Invoke(PythonNotFoundExitCode);
                return;
            }

            _runningProcess = process;
            _runningOnExit = onExit;
            _runningPoll = PollRunningProcess;
            EditorApplication.update += _runningPoll;
        }

        private static Process Start(string interpreter, string scriptPath, string workingDir, IEnumerable<string> scriptArguments)
        {
            var output = new StringBuilder();
            var error = new StringBuilder();
            var process = new Process();
            process.StartInfo = new ProcessStartInfo
            {
                FileName = interpreter,
                WorkingDirectory = workingDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true
            };
            process.StartInfo.ArgumentList.Add(scriptPath);
            if (scriptArguments != null)
            {
                foreach (var argument in scriptArguments)
                {
                    process.StartInfo.ArgumentList.Add(argument);
                }
            }
            process.OutputDataReceived += (_, args) =>
            {
                if (args.Data != null)
                {
                    output.AppendLine(args.Data);
                }
            };
            process.ErrorDataReceived += (_, args) =>
            {
                if (args.Data != null)
                {
                    error.AppendLine(args.Data);
                }
            };

            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            return process;
        }

        private static void PollRunningProcess()
        {
            var process = _runningProcess;
            if (process == null || !process.HasExited)
            {
                return;
            }

            var exitCode = process.ExitCode;
            var onExit = _runningOnExit;
            CleanupTracking();
            process.Dispose();
            onExit?.Invoke(exitCode);
        }

        private static void StopRunningProcess()
        {
            var process = _runningProcess;
            CleanupTracking();
            if (process == null)
            {
                return;
            }

            try
            {
                if (!process.HasExited)
                {
                    process.Kill();
                }
            }
            catch (Exception)
            {
                // Domain reload and process teardown must not throw into Unity.
            }
            finally
            {
                process.Dispose();
            }
        }

        private static void CleanupTracking()
        {
            if (_runningPoll != null)
            {
                EditorApplication.update -= _runningPoll;
            }

            _runningPoll = null;
            _runningProcess = null;
            _runningOnExit = null;
        }

        private static class PythonExecutable
        {
            // An editor launched from Unity Hub does not inherit the login shell's
            // PATH on macOS, so well-known install locations are tried first.
            private static readonly string[] MacCandidates =
            {
                "/opt/homebrew/bin/python3",
                "/usr/local/bin/python3",
                "python3",
                "/usr/bin/python3"
            };

            private static readonly string[] WindowsCandidates = { "python", "py", "python3" };

            private static readonly string[] LinuxCandidates = { "python3", "python" };

            internal static IEnumerable<string> Candidates()
            {
                switch (Application.platform)
                {
                    case RuntimePlatform.OSXEditor:
                        foreach (var candidate in MacCandidates)
                        {
                            if (!Path.IsPathRooted(candidate) || File.Exists(candidate))
                            {
                                yield return candidate;
                            }
                        }

                        break;
                    case RuntimePlatform.WindowsEditor:
                        foreach (var candidate in WindowsCandidates)
                        {
                            yield return candidate;
                        }

                        break;
                    default:
                        foreach (var candidate in LinuxCandidates)
                        {
                            yield return candidate;
                        }

                        break;
                }
            }
        }
    }
}
