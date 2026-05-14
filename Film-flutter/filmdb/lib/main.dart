import 'package:flutter/material.dart';
import 'services/api_service.dart';

void main() {
  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      home: TestScreen(),
    );
  }
}

class TestScreen extends StatefulWidget {
  const TestScreen({super.key});

  @override
  State<TestScreen> createState() => _TestScreenState();
}

class _TestScreenState extends State<TestScreen> {
  final ApiService apiService = ApiService();
  String result = 'Premi il bottone per testare';

  Future<void> testApi() async {
    try {
      final movies = await apiService.searchMovies('Batman');
      setState(() {
        result = 'Film trovati: ${movies.length}\nPrimo film: ${movies.isNotEmpty ? movies[0].title : "nessuno"}';
      });
    } catch (e) {
      setState(() {
        result = 'Errore: $e';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Test OMDb'),
      ),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(result),
        ),
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: testApi,
        child: const Icon(Icons.play_arrow),
      ),
    );
  }
}